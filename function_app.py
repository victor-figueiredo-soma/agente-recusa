import os
import azure.functions as func
import json

from models.schemas import EmailPayload, ProcessedEmail
from agents.email_analyzer import analyze_email
from agents.sheet_writer import write_to_sheet
from utils.logger import get_logger

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
logger = get_logger(__name__)


@app.route(route="email_webhook", methods=["POST"])
def email_webhook(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("Webhook recebido do Power Automate")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse("Payload JSON inválido", status_code=400)

    try:
        payload = EmailPayload(**body)
    except Exception as e:
        logger.error(f"Payload inválido: {e}")
        return func.HttpResponse(f"Payload inválido: {e}", status_code=422)

    try:
        analysis = analyze_email(payload)
    except Exception as e:
        logger.error(f"Erro na análise Gemini: {e}")
        return func.HttpResponse("Erro na análise do email", status_code=500)

    logger.info(f"is_recusa={analysis.is_recusa}, confianca={analysis.confianca}")

    result = {
        "status": "ok",
        "is_recusa": analysis.is_recusa,
        "confianca": analysis.confianca,
        "gravado_sheets": False,
        "reply_to": None,
        "reply_subject": None,
        "reply_body": None,
    }

    if analysis.is_recusa:
        remetente = f"{payload.fromName} <{payload.from_email}>" if payload.fromName else payload.from_email
        record = ProcessedEmail(
            message_id=payload.messageId,
            conversation_id=payload.conversationId,
            data_hora_recebimento=payload.receivedDateTime,
            remetente=remetente,
            assunto=payload.subject,
            nota_fiscal=analysis.nota_fiscal,
            motivo_recusa=analysis.motivo_recusa,
            confianca=analysis.confianca,
        )
        try:
            tipo_interacao = write_to_sheet(record)
            result["gravado_sheets"] = True
            result["tipo_interacao"] = tipo_interacao
        except Exception as e:
            logger.error(f"Erro ao gravar no Sheets: {e}")
            return func.HttpResponse("Erro ao gravar no Sheets", status_code=500)

        if tipo_interacao == "primeira":
            notification_email = os.environ.get("NOTIFICATION_EMAIL")
            if not notification_email:
                logger.warning("NOTIFICATION_EMAIL não configurada — auto-reply ignorado")
            else:
                nf = analysis.nota_fiscal or "não identificada"
                result["reply_to"] = notification_email  # guardrail: somente o email configurado
                result["reply_subject"] = f"[Chamado Criado] Recusa NF {nf} — {payload.subject}"
                result["reply_body"] = (
                    f"<p>Novo chamado de recusa registrado.</p>"
                    f"<ul>"
                    f"<li><strong>Nota Fiscal:</strong> {nf}</li>"
                    f"<li><strong>Transportadora/Remetente:</strong> {remetente}</li>"
                    f"<li><strong>Assunto:</strong> {payload.subject}</li>"
                    f"<li><strong>Motivo:</strong> {analysis.motivo_recusa or 'não identificado'}</li>"
                    f"<li><strong>Data de recebimento:</strong> {payload.receivedDateTime}</li>"
                    f"<li><strong>Id da Mensagem:</strong> {payload.messageId}</li>"
                    f"</ul>"
                    f"<p>Acesse o Sheets para visualizar o registro completo.</p>"
                )

    return func.HttpResponse(
        json.dumps(result),
        mimetype="application/json",
        status_code=200,
    )
