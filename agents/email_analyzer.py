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

O não-recebimento (chamado internamente de "recusa") ocorre por motivos como:
- Pedido cancelado pelo destinatário (ex: código "311-PEDIDO CANCELADO")
- Destinatário recusou por desconto comercial ou divergência de pedido
- Estabelecimento fechado no momento da entrega
- Prazo de entrega expirado
- Endereço não encontrado ou destinatário ausente
- Devolução automática por ausência de instrução

PADRÕES COMUNS DE TRANSPORTADORAS:
- "mercadoria(s) a nós confiada(s) para transporte"
- "encontra(m)-se pendente(s), motivado(s) pela seguinte ocorrência"
- "consideraremos como DEVOLUÇÃO AUTOMÁTICA"
- Códigos de ocorrência no formato "NNN-DESCRIÇÃO" (ex: "311-PEDIDO CANCELADO")
- Referências por NF, CTE ou AWB

TAREFA:
Determine se a comunicação é uma notificação de não-entrega. Extraia o(s) número(s) de Nota
Fiscal e o motivo. Se houver múltiplas NFs, retorne todas separadas por vírgula.
Normalize códigos de ocorrência para linguagem clara (ex: "311-PEDIDO CANCELADO" → "Pedido cancelado").

Responda SOMENTE com um objeto JSON válido, sem texto adicional, seguindo exatamente este schema:
{
  "is_recusa": <true se for notificação de não-entrega, false caso contrário>,
  "nota_fiscal": "<número(s) da NF mencionado(s), separados por vírgula se houver mais de um, ou null se não identificado>",
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

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
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
