import os
import json
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from models.schemas import EmailPayload, ProcessedEmail
from agents.email_analyzer import analyze_email
from agents.sheet_writer import write_to_sheet
from utils.logger import get_logger

app = FastAPI()
logger = get_logger(__name__)

_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/email_webhook")
async def email_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
):
    if _WEBHOOK_SECRET and x_webhook_secret != _WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido")

    try:
        payload = EmailPayload(**body)
    except Exception as e:
        logger.error(f"Payload inválido: {e}")
        raise HTTPException(status_code=422, detail=str(e))

    try:
        analysis = analyze_email(payload)
    except Exception as e:
        logger.error(f"Erro na análise Gemini: {e}")
        raise HTTPException(status_code=500, detail="Erro na análise do email")

    logger.info(f"is_recusa={analysis.is_recusa}, confianca={analysis.confianca}")

    result = {
        "status": "ok",
        "is_recusa": analysis.is_recusa,
        "confianca": analysis.confianca,
        "gravado_sheets": False,
        "tipo_interacao": "",
        "send_reply": False,
        "reply_to": "",
        "reply_subject": "",
        "reply_body": "",
    }

    if analysis.is_recusa:
        remetente = f"{payload.fromName} <{payload.from_email}>" if payload.fromName else payload.from_email
        record = ProcessedEmail(
            message_id=payload.messageId,
            conversation_id=payload.conversationId,
            data_hora_recebimento=payload.receivedDateTime,
            remetente=remetente,
            transportadora=analysis.transportadora,
            tipo_mensagem=analysis.tipo_mensagem,
            assunto=payload.subject,
            nota_fiscal=analysis.nota_fiscal,
            motivo_recusa=analysis.motivo_recusa,
            confianca=analysis.confianca,
        )
        try:
            tipo_interacao = write_to_sheet(record)
            result["gravado_sheets"] = (tipo_interacao == "primeira")
            result["tipo_interacao"] = tipo_interacao
        except Exception as e:
            logger.error(f"Erro ao gravar no Sheets: {e}")
            raise HTTPException(status_code=500, detail="Erro ao gravar no Sheets")

        if tipo_interacao == "primeira":
            notification_email = os.environ.get("NOTIFICATION_EMAIL")
            if not notification_email:
                logger.warning("NOTIFICATION_EMAIL não configurada — auto-reply ignorado")
            else:
                nf = analysis.nota_fiscal or "não identificada"
                result["send_reply"] = True
                result["reply_to"] = notification_email
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

    return JSONResponse(content=result)
