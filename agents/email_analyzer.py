import os
import re
import html
import json
from google import genai
from google.genai import types
from models.schemas import EmailPayload, AnalysisResult
from utils.logger import get_logger


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

logger = get_logger(__name__)

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

PASSO 5 — Extrair o motivo da recusa
  - Normalizar código de ocorrência ou linguagem livre para descrição clara e objetiva
    (ex: "311-PEDIDO CANCELADO" → "Pedido cancelado";
         "não recebeu devido a desconto comercial" → "Desconto comercial")
  - Se is_recusa = false → motivo_recusa = null

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
     ou solicitação de autorização de reentrega
  Se qualquer critério não for satisfeito → is_recusa = false

PASSO 8 — Classificar o tipo de mensagem
  "padrao_automatico" → estrutura rígida e padronizada, gerada automaticamente (Braspress, Movvi)
  "mensagem_livre"    → formato livre ou semi-estruturado, redigido manualmente (Solução, Comboio)

PASSO 9 — Definir confiança
  "alta"  → remetente conhecido + NF clara + estrutura reconhecível
  "media" → um ou mais campos com ambiguidade mas classificação possível
  "baixa" → múltiplas incertezas ou email atípico

Responda SOMENTE com um objeto JSON válido, sem texto adicional, seguindo exatamente este schema:
{
  "is_recusa": <true se for notificação de não-entrega, false caso contrário>,
  "transportadora": "<nome normalizado da transportadora, ou null se não identificado>",
  "nota_fiscal": "<7 primeiros dígitos da(s) NF(s), separados por vírgula se houver mais de uma, ou null se não identificado>",
  "motivo_recusa": "<motivo da não-entrega em linguagem clara e objetiva, ou null se não for recusa>",
  "confianca": "<'alta', 'media' ou 'baixa' — sua confiança na classificação>",
  "tipo_mensagem": "<'padrao_automatico' ou 'mensagem_livre'>"
}
"""


def analyze_email(payload: EmailPayload, thread_history: list[dict] | None = None) -> AnalysisResult:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY não configurada")

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)

    user_message = (
        f"Assunto: {payload.subject}\n"
        f"Remetente: {payload.fromName or ''} <{payload.from_email}>\n"
        f"Data: {payload.receivedDateTime}\n\n"
        f"Corpo:\n{_strip_html(_strip_quoted_html(payload.body))}"
    )

    if thread_history:
        lines = ["\n\n=== HISTÓRICO DA THREAD (mensagens anteriores, mais recente primeiro) ==="]
        for i, msg in enumerate(thread_history, 1):
            addr = msg.get("from", {}).get("emailAddress", {})
            sender = f"{addr.get('name', '')} <{addr.get('address', '')}>".strip()
            snippet = _strip_html(msg.get("body", {}).get("content", ""))[:300]
            lines.append(
                f"\n[{i}] De: {sender} | Data: {msg.get('receivedDateTime', '')}\n"
                f"    Assunto: {msg.get('subject', '')}\n"
                f"    Corpo: {snippet}{'...' if len(snippet) == 300 else ''}"
            )
        user_message += "\n".join(lines)

    logger.info(f"Analisando email: '{payload.subject}' de {payload.from_email}")
    logger.info(f"Conteúdo enviado ao Gemini:\n{user_message[:1000]}")

    response = client.models.generate_content(
        model=model_name,
        contents=user_message,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )

    raw = response.text.strip()
    logger.info(f"Resposta Gemini: {raw}")

    try:
        from utils import pricing, supabase_client
        usage = response.usage_metadata
        if usage:
            events = pricing.gemini_cost_brl(
                prompt_tokens=usage.prompt_token_count or 0,
                thinking_tokens=getattr(usage, "thoughts_token_count", None) or 0,
                output_tokens=usage.candidates_token_count or 0,
            )
            for event in events:
                supabase_client.insert_usage_event(
                    origem="gemini",
                    email_id=payload.messageId,
                    **event,
                )
    except Exception as e:
        logger.warning(f"Falha ao registrar uso Gemini: {e}")

    data = json.loads(raw)
    return AnalysisResult(**data)
