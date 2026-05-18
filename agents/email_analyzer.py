import os
import re
import html
import json
from google import genai
from google.genai import types
from models.schemas import EmailPayload, AnalysisResult
from utils.logger import get_logger


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
- Para Solução e Comboio: identificar pelo nome na assinatura ou corpo do email

TAREFA:
Analise o assunto e o remetente primeiro como sinais primários, depois confirme no corpo.

Regras de extração:
- TRANSPORTADORA: identifique pelo domínio do remetente (Braspress, Movvi) ou pelo nome mencionado
  no corpo/assinatura (Solução, Comboio). Retorne apenas o nome normalizado (ex: "Braspress", "Movvi",
  "Solução", "Comboio"). Se não identificado, retorne null.
- NOTA FISCAL: extraia os 7 primeiros dígitos numéricos do número da NF, ignorando sub-séries
  como "/72" ou qualquer sufixo após o 7º dígito. Se houver múltiplas NFs, retorne todas
  separadas por vírgula (ex: "1528101, 1527451").
- MOTIVO: normalize códigos de ocorrência ou linguagem livre para descrição clara
  (ex: "311-PEDIDO CANCELADO" → "Pedido cancelado"; "não recebeu devido a desconto comercial" → "Desconto comercial").
- TIPO DE MENSAGEM: classifique o formato do email:
  "padrao_automatico" → emails com estrutura rígida e padronizada enviados automaticamente (Braspress, Movvi)
  "mensagem_livre"    → emails com formato livre ou semi-estruturado, redigidos manualmente (Solução, Comboio)

Responda SOMENTE com um objeto JSON válido, sem texto adicional, seguindo exatamente este schema:
{
  "is_recusa": <true se for notificação de não-entrega, false caso contrário>,
  "transportadora": "<nome normalizado da transportadora, ou null se não identificado>",
  "nota_fiscal": "<7 primeiros dígitos da(s) NF(s), separados por vírgula se houver mais de uma, ou null se não identificado>",
  "motivo_recusa": "<motivo da não-entrega em linguagem clara e objetiva, ou null se não for recusa>",
  "confianca": "<'alta', 'media' ou 'baixa' — sua confiança na classificação>",
  "tipo_mensagem": "<'padrao_automatico' ou 'mensagem_livre'>"
}

Critérios para is_recusa = true:
- Transportadora informando falha, pendência ou impossibilidade de entrega
- Comunicado de devolução automática por não-entrega
- Notificação de recusa de recebimento pelo destinatário (lojista)
- Solicitação de autorização de reentrega após recusa

Critérios para is_recusa = false:
- Email não relacionado a logística ou entrega
- Confirmação de entrega bem-sucedida
- Emails administrativos, financeiros ou comerciais sem relação com entrega
- Spam ou automático sem conteúdo de entrega
"""


def analyze_email(payload: EmailPayload) -> AnalysisResult:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY não configurada")

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=api_key)

    user_message = (
        f"Assunto: {payload.subject}\n"
        f"Remetente: {payload.fromName or ''} <{payload.from_email}>\n"
        f"Data: {payload.receivedDateTime}\n\n"
        f"Corpo:\n{_strip_html(payload.body)}"
    )

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

    data = json.loads(raw)
    return AnalysisResult(**data)
