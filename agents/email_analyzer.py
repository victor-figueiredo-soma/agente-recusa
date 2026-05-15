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
Você analisa emails enviados por transportadoras para uma equipe comercial de atacado de moda.
Esses emails notificam que a transportadora tentou entregar uma caixa de produtos (identificada
por uma Nota Fiscal) a um lojista multimarca, mas a entrega não foi realizada com sucesso.

O não-recebimento (chamado internamente de "recusa") pode ocorrer por diversos motivos:
- O lojista recusou fisicamente a entrega (não quer receber a mercadoria)
- O prazo de entrega expirou e o lojista não aceita mais o pedido
- A loja estava fechada no momento da tentativa de entrega
- Endereço não encontrado ou destinatário ausente
- Outros impedimentos operacionais da entrega

TAREFA:
Analise o email e determine se trata-se de uma notificação de não-entrega de uma transportadora.
Extraia o número da Nota Fiscal e o motivo pelo qual a entrega não foi realizada.

Responda SOMENTE com um objeto JSON válido, sem texto adicional, seguindo exatamente este schema:
{
  "is_recusa": <true se for notificação de não-entrega de transportadora, false caso contrário>,
  "nota_fiscal": "<número da NF ou pedido mencionado no email, ou null se não identificado>",
  "motivo_recusa": "<motivo pelo qual a entrega não foi realizada, descrito de forma objetiva, ou null se não for recusa>",
  "confianca": "<'alta', 'media' ou 'baixa' — sua confiança na classificação>"
}

Critérios para is_recusa = true:
- Email de transportadora informando falha ou impossibilidade de entrega
- Comunicado de devolução de mercadoria ao remetente por não-entrega
- Notificação de recusa de recebimento pelo destinatário (lojista)

Critérios para is_recusa = false:
- Email não relacionado a logística ou entrega
- Email de confirmação de entrega bem-sucedida
- Emails administrativos, financeiros ou comerciais sem relação com entrega
- Spam ou email automático sem conteúdo de entrega
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
