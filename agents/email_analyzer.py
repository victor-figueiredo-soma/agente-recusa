import os
import re
import html
import json
import threading
from google import genai
from google.genai import types
from models.schemas import EmailPayload, AnalysisResult, SUBMOTIVOS, SUBMOTIVO_FALLBACK
from utils.logger import get_logger
from utils.retry import transient_retry

_genai_client: genai.Client | None = None
_genai_lock = threading.Lock()


def _get_genai_client() -> genai.Client:
    """Cliente Gemini em cache (singleton) — evita reinstanciar a cada e-mail."""
    global _genai_client
    if _genai_client is None:
        with _genai_lock:
            if _genai_client is None:
                api_key = os.environ.get("GEMINI_API_KEY")
                if not api_key:
                    raise ValueError("GEMINI_API_KEY não configurada")
                _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def _strip_quoted_html(text: str) -> str:
    """Remove blocos de conteúdo citado de replies antes de processar o HTML."""
    # Padrão universal: <blockquote> (Gmail, Outlook, etc.)
    text = re.sub(r'<blockquote[^>]*>.*?</blockquote>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Outlook Web/Graph API: div com id="divRplyFwdMsg" e o que vem depois
    text = re.sub(r'<div[^>]*id=["\']divRplyFwdMsg["\'][^>]*>.*', '', text, flags=re.DOTALL | re.IGNORECASE)
    return text


def _strip_html(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


# Indícios de que o texto contém a notificação da transportadora (e não só uma
# resposta curta tipo "favor verificar").
_CARRIER_MARKERS = (
    "nota fiscal", "nf ", "notas fiscais", "cte", "ct-e", "conhecimento",
    "pendente", "pendência", "pendencia", "ocorrência", "ocorrencia", "awb",
)


def _has_carrier_signal(text: str) -> bool:
    if len(text) < 150:  # curto demais para ser uma notificação de pendência
        return False
    low = text.lower()
    return any(m in low for m in _CARRIER_MARKERS)


def _body_for_analysis(raw_html: str) -> str:
    """Corpo a enviar ao Gemini.

    Prefere o texto SEM citação (a mensagem nova) — comportamento ideal para
    e-mails diretos da transportadora. Mas se esse texto for curto demais ou não
    tiver indícios de notificação (NF/CT-e/ocorrência), faz fallback para o corpo
    COMPLETO com citações: é o caso das pendências encaminhadas/respondidas, em que
    a notificação original (NF, CT-e, motivo) só existe no trecho citado.

    Sem truncamento: leva todo o conteúdo disponível."""
    dequoted = _strip_html(_strip_quoted_html(raw_html))
    if _has_carrier_signal(dequoted):
        return dequoted
    return _strip_html(raw_html)


logger = get_logger(__name__)

# Lista formatada para injetar no prompt (uma categoria por linha). Mantém o prompt
# sincronizado com a fonte única SUBMOTIVOS de schemas.py.
_SUBMOTIVOS_TEXT = "\n".join(f"    - {s}" for s in SUBMOTIVOS)

_SYSTEM_PROMPT = """
Você é um especialista em logística e operações comerciais do setor de moda multimarcas.

CONTEXTO:
Você analisa comunicações recebidas pela equipe de atacado de moda notificando que uma
transportadora tentou entregar caixas de produtos (identificadas por Nota Fiscal) a lojistas
multimarca, mas a entrega não foi concluída. As comunicações são sempre diretas das transportadoras.

=== TRANSPORTADORA: BRASPRESS — formato padrao_automatico ===
Estrutura característica:
- Cabeçalho: "BRASPRESS TRANSPORTES URGENTES LTDA" seguido de endereço
- Título: "COMUNICAÇÃO DE PENDÊNCIAS"
- Frase-chave: "Vimos, pela presente, notificá-los, que a(s) mercadoria(s) a nós confiada(s)
  para transporte, através da(s) Notas(s) Fiscal(is): [NF], acobertada(s) pelo conhecimento
  Numero(AWB) : [AWB] emitido em [DATA] ([FILIAL]), destinatário [DESTINATÁRIO],
  encontra(m)-se pendente(s), motivado(s) pela seguinte ocorrência : [CÓDIGO-DESCRIÇÃO]"
- NF: logo após "Notas(s) Fiscal(is):" — pode conter barra (ex: "1528101/72")
- Ocorrência: código numérico seguido de descrição (ex: "311-PEDIDO CANCELADO")
- Contato: domínio @braspress.com.br
- Alerta: sempre menciona devolução automática e cobrança de frete (50% rodoviário, 30% rodoaéreo)

=== TRANSPORTADORA: MOVVI — formato padrao_automatico ===
Estrutura característica:
- Frase-chave: "A mercadoria a nós confiada para transporte, através do CTE [CTE], NF [NF]
  emitido em [DATA] tendo como destinatário [DESTINATÁRIO] encontra-se pendente de entrega
  em razão da seguinte ocorrência:"
- Ocorrência: linha seguinte em caixa alta (ex: "ESTABELECIMENTO FECHADO")
- NF: após "NF " na frase-chave
- Contato: domínio @movvi.com.br
- Alerta: menciona devolução automática e cobrança de 100% do frete

=== TRANSPORTADORA: SOLUÇÃO — formato mensagem_livre ===
Estrutura característica:
- Mensagens menos padronizadas, redigidas manualmente pelo operador da transportadora
- Assunto frequentemente contém número(s) de NF e nome do destinatário (ex: "NFs 1527451 e 1527590 recusada E Flores Curitiba")
- Corpo em linguagem conversacional: "As NFs XXXXXX da [DESTINATÁRIO] foi recusada, o destinatário informa que não recebeu devido a [MOTIVO]"
- Identificação: nome "SOLUÇÃO" ou "SOLUCAO" ou "SOLUÇAO" aparece na assinatura ou no corpo do email
- Pode não ter código de ocorrência — o motivo é descrito em linguagem livre

=== TRANSPORTADORA: COMBOIO — formato mensagem_livre ===
Estrutura característica:
- Mensagens semi-estruturadas, com campos como "Remetente:", "Destinatário:", "Ocorrência:" mas em formato mais livre
- Exemplo de assunto: "NF [NÚMERO] [DESTINATÁRIO] (Ocorrência)"
- Corpo típico: "Segue comunicado de ocorrência; Remetente: [X]; Destinatário: [Y]; Ocorrência: RECUSADO EM [DATA], [DETALHES]. Gentileza informar se está autorizado reentrega."
- Identificação: nome "COMBOIO" aparece na assinatura ou no corpo do email
- Solicita confirmação de reentrega ou instrução do remetente

=== DISTINÇÃO: RECUSA vs RETENÇÃO FISCAL vs VOLUME TROCADO/INCORRETO ===

RECUSA: destinatário (lojista) recusou ou não aceitou a mercadoria, ou a transportadora
não conseguiu efetuar a entrega por motivo operacional (endereço errado, estabelecimento
fechado, pedido cancelado pelo destinatário, carga não reconhecida, etc.).

RETENÇÃO FISCAL: mercadoria retida por órgão governamental ou problema documental fiscal.
Sinais: menção a SEFAZ, Receita Federal, posto fiscal, barreira fiscal, DANFE inválido,
NF-e com divergência, apreensão fiscal, "retida em barreiras fiscais", ICMS não recolhido,
documentação fiscal irregular, retenção tributária, etc.

EXTRAVIO: mercadoria perdida, extraviada ou não localizada pela transportadora.
Sinais: palavras como "extravio", "extraviado", "mercadoria não localizada", "carga perdida",
"sinistro de extravio", "não encontrada no sistema", "paradeiro desconhecido", etc.

VOLUME TROCADO / VOLUME INCORRETO: erro de expedição — a transportadora ou o remetente
enviou caixas/volumes errados ao destinatário (volumes destinados a outro cliente, produtos
trocados, quantidade incorreta enviada, etc.). NÃO é recusa. O destinatário não recusou
a entrega; o problema é do remetente ou da transportadora na separação/envio.
Sinais: "volume trocado", "volumes trocados", "volume incorreto", "volumes incorretos",
"caixas trocadas", "produto errado", "mercadoria trocada", "enviado por engano", "entregue
errado", "não corresponde ao pedido", "não é o nosso produto", "produtos de outro cliente", etc.
→ is_recusa = false SEMPRE para estes casos.

=== SINAIS PRIMÁRIOS DE IDENTIFICAÇÃO ===
Assunto do email para BRASPRESS e MOVVI:
- "COMUNICAÇÃO DE PENDÊNCIAS", "Comunicado de Pendência", "Pendência de Entrega", "Aviso de Pendência"
Se o assunto contiver qualquer dessas expressões, trate como forte indicador de is_recusa = true.

Assunto do email para SOLUÇÃO e COMBOIO:
- Contém número(s) de NF (sequência de 7 dígitos) e/ou nome de destinatário
- Pode conter palavras como "recusada", "ocorrência", "pendência"

Remetente (domínio do email):
- @braspress.com.br → Braspress
- @movvi.com.br → Movvi
- @jadlog.com.br → is_recusa = false SEMPRE. JADLOG nunca envia notificações de recusa para este mailbox.
- Para Solução e Comboio: identificar pelo nome na assinatura ou corpo do email

=== PASSO A PASSO DE ANÁLISE ===

Siga esta sequência obrigatoriamente a cada email recebido:

PASSO 1 — Verificar o domínio do remetente (exclusão imediata)
  - @jadlog.com.br → is_recusa = false. Encerre a análise aqui.
  - @braspress.com.br → transportadora = "Braspress" (confirmar no corpo)
  - @movvi.com.br → transportadora = "Movvi" (confirmar no corpo)
  - Outros domínios → transportadora será identificada no corpo/assinatura (Passo 3)

PASSO 2 — Verificar o assunto (sinais primários, não conclusivos)
  - "COMUNICAÇÃO DE PENDÊNCIAS", "Comunicado de Pendência", "Pendência de Entrega",
    "Aviso de Pendência" → forte indicador de is_recusa = true; confirmar no corpo
  - "[CADASTROS", "Solicitações", "STATUS de lojas", "mudança de STATUS" → forte indicador
    de is_recusa = false; confirmar no corpo
  - Palavras como "recusada", "ocorrência", "pendência" → indicador para Solução/Comboio
  - NÃO extraia NF do assunto a menos que o número esteja explicitamente precedido de
    "NF", "NFs" ou "Nota Fiscal" (ex: "NFs 1527451 e 1527590 — E Flores Curitiba")
  - Números isolados no assunto sem essa identificação devem ser ignorados para fins de NF

PASSO 3 — Analisar o corpo do email
  - Identificar se é uma notificação de falha ou impossibilidade de entrega
  - Identificar transportadora pelo nome na assinatura ou corpo (Solução, Comboio), se
    não resolvida no Passo 1
  - Confirmação de entrega bem-sucedida → is_recusa = false
  - Email administrativo, financeiro, de cadastro ou operacional interno → is_recusa = false
  - Emails de transportadoras sobre assuntos operacionais que não sejam notificação de falha
    de entrega (ex: relatórios, templates, atualizações de status de loja, comunicados gerais)
    → is_recusa = false
  - "Devolutivas" no contexto de cadastro de lojas ou processos comerciais NÃO é recusa
    de entrega → is_recusa = false
  - Spam ou email automático sem conteúdo de entrega → is_recusa = false
  - Notificação de volume trocado, volume incorreto ou erro de expedição (caixas/produtos
    errados enviados ao destinatário) → is_recusa = false

PASSO 4 — Extrair a Nota Fiscal (obrigatório para is_recusa = true)
  - Braspress: localizar "Notas(s) Fiscal(is):" e extrair os 7 dígitos antes da barra;
    "/72" e similares são sub-série e devem ser descartados (ex: "1528101/72" → "1528101")
  - Movvi: localizar "NF " na frase-chave e extrair os 7 dígitos do número da NF
  - Solução / Comboio: buscar "NF", "NFs" ou "Nota Fiscal" seguido de número de 7 dígitos
    no corpo; NÃO usar números do assunto que não tenham essa identificação explícita
  - Se houver múltiplas NFs, retornar todas separadas por vírgula (ex: "1528101, 1527451")
  - A NF deve ter exatamente 7 dígitos. Números com menos ou mais de 7 dígitos NÃO são NF
    e devem ser ignorados (ex: "123456" ou "12345678" → ignorar)
  - Se nenhuma NF for identificada → nota_fiscal = null → is_recusa = false (regra absoluta)

PASSO 5 — Extrair o motivo da recusa (descrição livre)
  - Normalizar código de ocorrência ou linguagem livre para descrição clara e objetiva
    (ex: "311-PEDIDO CANCELADO" → "Pedido cancelado";
         "não recebeu devido a desconto comercial" → "Desconto comercial")
  - Se is_recusa = false → motivo_recusa = null

PASSO 5B — Classificar o SUB-MOTIVO padronizado (obrigatório quando is_recusa = true)
  Enquadre a ocorrência em EXATAMENTE UMA das categorias padronizadas abaixo. O valor
  deve ser retornado em CAIXA ALTA, idêntico à lista (com underscores), sem inventar
  variações. Esta categoria é controlada e alimenta a deduplicação — não improvise.

  Categorias permitidas (sub_motivo):
__SUBMOTIVOS_LIST__

  Orientação de mapeamento (exemplos, não exaustivo):
  - "PEDIDO CANCELADO" / código 311 → PEDIDO_CANCELADO
  - "ESTABELECIMENTO FECHADO" / loja fechada → ESTABELECIMENTO_FECHADO
  - mudança de endereço do destinatário → MUDOU_DE_ENDERECO
  - transportadora não atende a região → TRANSPORTADORA_NAO_ATENDE_REGIAO
  - fora do prazo de entrega / prazo expirado → FORA_DO_PRAZO
  - grade/produto quebrado ou danificado → GRADE_QUEBRADA
  - caixa lacrada com falta de peça → FALTA_PECA_CAIXA_LACRADA
  - caixa avariada com falta de peça → FALTA_PECA_CAIXA_COM_AVARIA
  - extravio total da carga → EXTRAVIO_TOTAL ; extravio de parte → EXTRAVIO_PARCIAL
  - sobra de volume → SOBRA_DE_VOLUME ; troca de volume → TROCA_DE_VOLUME
  - faturado para CNPJ errado → FATURADO_PARA_CNPJ_INCORRETO
  - retenção/aguardando ICMS antecipado → AGUARDANDO_PGTO_ICMS_ANTECIPADO
  - acordo comercial / desconto comercial → ACORDO_COMERCIAL
  - promoção do site → PROMOCAO_DO_SITE
  - valor de recebimento alto → VALOR_DE_RECEBIMENTO_ALTO
  - entrega de fato realizada → ENTREGA_REALIZADA
  - REGRA FIXA: sempre que status = "RETENÇÃO FISCAL" → sub_motivo = AGUARDANDO_PGTO_ICMS_ANTECIPADO
  - SE NÃO HOUVER ENQUADRAMENTO CLARO → __FALLBACK__
  - Se is_recusa = false → sub_motivo = null

PASSO 6 — Considerar o histórico da thread (quando fornecido)
  - O campo "HISTÓRICO DA THREAD" contém mensagens anteriores em ordem cronológica
    decrescente (mais recente primeiro)
  - Usar para: entender se é continuidade de notificação já enviada; identificar se já
    houve resposta do time de logística; confirmar ou ajustar a classificação final

PASSO 7 — Determinar is_recusa
  TODOS os critérios abaixo devem ser satisfeitos para is_recusa = true:
  1. Transportadora notificando falha, pendência ou impossibilidade de entrega de remessa específica
  2. Nota Fiscal identificável no corpo (7 dígitos) — obrigatório
  3. Comunicado de devolução automática por não-entrega, recusa pelo destinatário (lojista),
     solicitação de autorização de reentrega, OU retenção fiscal/documental da mercadoria
  Se qualquer critério não for satisfeito → is_recusa = false

  EXCLUSÕES ABSOLUTAS (is_recusa = false independentemente de qualquer outro sinal):
  - Volume trocado / volume incorreto / erro de expedição: quando o problema é que foram
    enviados volumes ou produtos errados ao destinatário (erro do remetente/transportadora
    na separação), e NÃO uma recusa de entrega pelo lojista.

PASSO 8 — Classificar o tipo de mensagem
  "padrao_automatico" → estrutura rígida e padronizada, gerada automaticamente (Braspress, Movvi)
  "mensagem_livre"    → formato livre ou semi-estruturado, redigido manualmente (Solução, Comboio)

PASSO 9 — Definir confiança
  "alta"  → remetente conhecido + NF clara + estrutura reconhecível
  "media" → um ou mais campos com ambiguidade mas classificação possível
  "baixa" → múltiplas incertezas ou email atípico

PASSO 10 — Classificar status
  "RECUSA"           → recusa operacional pelo destinatário ou impossibilidade de entrega
  "RETENÇÃO FISCAL"  → mercadoria retida por órgão fiscal ou problema documental fiscal
  "EXTRAVIO"         → mercadoria perdida ou não localizada pela transportadora
  Se is_recusa = false → status = null

Responda SOMENTE com um objeto JSON válido, sem texto adicional, seguindo exatamente este schema:
{
  "is_recusa": <true se for notificação de não-entrega ou retenção fiscal, false caso contrário>,
  "transportadora": "<nome normalizado da transportadora, ou null se não identificado>",
  "nota_fiscal": "<7 primeiros dígitos da(s) NF(s), separados por vírgula se houver mais de uma, ou null se não identificado>",
  "motivo_recusa": "<motivo da não-entrega em linguagem clara e objetiva, ou null se não for recusa>",
  "sub_motivo": "<UMA das categorias padronizadas em CAIXA ALTA (PASSO 5B), ou null se is_recusa = false>",
  "confianca": "<'alta', 'media' ou 'baixa' — sua confiança na classificação>",
  "tipo_mensagem": "<'padrao_automatico' ou 'mensagem_livre'>",
  "status": "<'RECUSA', 'RETENÇÃO FISCAL' ou 'EXTRAVIO', ou null se is_recusa = false>"
}
""".replace("__SUBMOTIVOS_LIST__", _SUBMOTIVOS_TEXT).replace("__FALLBACK__", SUBMOTIVO_FALLBACK)


@transient_retry
def _generate(client, model_name: str, user_message: str):
    """Chamada ao Gemini com retry transitório (429/5xx/timeout) — backoff
    exponencial, 3 tentativas. Sem o retry, uma instabilidade do Gemini faz o
    e-mail ser descartado sem reprocessamento."""
    return client.models.generate_content(
        model=model_name,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )


def analyze_email(payload: EmailPayload, thread_history: list[dict] | None = None) -> AnalysisResult:
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    client = _get_genai_client()

    user_message = (
        f"Assunto: {payload.subject}\n"
        f"Remetente: {payload.fromName or ''} <{payload.from_email}>\n"
        f"Data: {payload.receivedDateTime}\n\n"
        f"Corpo:\n{_body_for_analysis(payload.body)}"
    )

    if thread_history:
        lines = ["\n\n=== HISTÓRICO DA THREAD (mensagens anteriores, mais recente primeiro) ==="]
        for i, msg in enumerate(thread_history, 1):
            addr = msg.get("from", {}).get("emailAddress", {})
            sender = f"{addr.get('name', '')} <{addr.get('address', '')}>".strip()
            raw = msg.get("body", {}).get("content", "")
            # Opção 1 — leva tudo, sem duplicar: a mensagem mais recente (i == 1)
            # já carrega a cadeia inteira citada (inclui originais e encaminhados),
            # então vai com o corpo COMPLETO. As demais entram só com o texto novo
            # (de-quotado), para não repetir a mesma cadeia N vezes. Sem truncamento.
            corpo = _strip_html(raw) if i == 1 else _strip_html(_strip_quoted_html(raw))
            lines.append(
                f"\n[{i}] De: {sender} | Data: {msg.get('receivedDateTime', '')}\n"
                f"    Assunto: {msg.get('subject', '')}\n"
                f"    Corpo: {corpo}"
            )
        user_message += "\n".join(lines)

    logger.info(f"Analisando email: '{payload.subject}' de {payload.from_email}")
    logger.info(f"Conteúdo enviado ao Gemini:\n{user_message[:1000]}")

    response = _generate(client, model_name, user_message)

    raw = response.text.strip()
    logger.info(f"Resposta Gemini: {raw}")

    try:
        from utils import pricing
        from agents import bq_client
        usage = response.usage_metadata
        if usage:
            events = pricing.gemini_cost_brl(
                prompt_tokens=usage.prompt_token_count or 0,
                thinking_tokens=getattr(usage, "thoughts_token_count", None) or 0,
                output_tokens=usage.candidates_token_count or 0,
            )
            for event in events:
                bq_client.insert_usage_event(
                    origem="gemini",
                    thread_id=payload.conversationId,
                    **event,
                )
    except Exception as e:
        logger.warning(f"Falha ao registrar uso Gemini: {e}")

    data = json.loads(raw)
    return AnalysisResult(**data)
