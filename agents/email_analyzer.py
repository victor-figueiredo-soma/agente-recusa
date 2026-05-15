import os
import json
from google import genai
from google.genai import types
from models.schemas import EmailPayload, AnalysisResult
from utils.logger import get_logger

logger = get_logger(__name__)

_SYSTEM_PROMPT = """
Você é um especialista em logística e operações comerciais do setor de moda multimarcas.

CONTEXTO:
Você analisa comunicações recebidas pela equipe de atacado de moda notificando que uma
transportadora tentou entregar caixas de produtos (identificadas por Nota Fiscal) a lojistas
multimarca, mas a entrega não foi concluída. Essas comunicações chegam como email direto de
transportadoras ou como mensagens internas de equipe (Teams, WhatsApp, etc.) relatando recusas.

=== TRANSPORTADORA: BRASPRESS ===
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

=== TRANSPORTADORA: MOVVI ===
Estrutura característica:
- Frase-chave: "A mercadoria a nós confiada para transporte, através do CTE [CTE], NF [NF]
  emitido em [DATA] tendo como destinatário [DESTINATÁRIO] encontra-se pendente de entrega
  em razão da seguinte ocorrência:"
- Ocorrência: linha seguinte em caixa alta (ex: "ESTABELECIMENTO FECHADO")
- NF: após "NF " na frase-chave
- Contato: domínio @movvi.com.br
- Alerta: menciona devolução automática e cobrança de 100% do frete

=== OUTROS PADRÕES ===
- Mensagens internas de equipe (Teams/WhatsApp) relatando recusas: "NF XXXXXX foi recusada"
- Motivos comuns: pedido cancelado, estabelecimento fechado, desconto comercial, prazo expirado,
  endereço não encontrado, destinatário ausente

=== SINAIS PRIMÁRIOS DE IDENTIFICAÇÃO ===
Assunto do email: sempre conterá variações de "Comunicado de Pendência", como:
- "COMUNICAÇÃO DE PENDÊNCIAS"
- "Comunicado de Pendência"
- "Pendência de Entrega"
- "Aviso de Pendência"
- "Notificação de Pendência"
Se o assunto contiver qualquer dessas expressões, trate como forte indicador de is_recusa = true.

Remetente: o domínio do email identifica a transportadora:
- @braspress.com.br → Braspress
- @movvi.com.br → Movvi
- Outros domínios de transportadoras também são válidos

TAREFA:
Analise o assunto e o remetente primeiro como sinais primários, depois confirme no corpo.

Regras de extração:
- TRANSPORTADORA: identifique pelo nome mencionado no corpo do email (ex: "BRASPRESS", "MOVVI").
  Retorne apenas o nome normalizado (ex: "Braspress", "Movvi").
- NOTA FISCAL: extraia os 7 primeiros dígitos numéricos do número da NF, ignorando sub-séries
  como "/72" ou qualquer sufixo após o 7º dígito. Se houver múltiplas NFs, retorne todas
  separadas por vírgula (ex: "1528101, 1527451").
- MOTIVO: normalize códigos de ocorrência para linguagem clara
  (ex: "311-PEDIDO CANCELADO" → "Pedido cancelado").

Responda SOMENTE com um objeto JSON válido, sem texto adicional, seguindo exatamente este schema:
{
  "is_recusa": <true se for notificação de não-entrega, false caso contrário>,
  "transportadora": "<nome da transportadora identificado no corpo, ou null se não identificado>",
  "nota_fiscal": "<7 primeiros dígitos da(s) NF(s), separados por vírgula se houver mais de uma, ou null se não identificado>",
  "motivo_recusa": "<motivo da não-entrega em linguagem clara e objetiva, ou null se não for recusa>",
  "confianca": "<'alta', 'media' ou 'baixa' — sua confiança na classificação>"
}

Critérios para is_recusa = true:
- Transportadora informando falha, pendência ou impossibilidade de entrega
- Comunicado de devolução automática por não-entrega
- Notificação de recusa de recebimento pelo destinatário (lojista)
- Mensagem interna de equipe relatando que NF foi recusada ou não recebida

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
        f"Corpo:\n{payload.body}"
    )

    logger.info(f"Analisando email: '{payload.subject}' de {payload.from_email}")

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
