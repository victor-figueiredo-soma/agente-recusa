import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from models.schemas import EmailPayload, ProcessedEmail, GraphNotificationPayload
from agents.email_analyzer import analyze_email
from agents.sheet_writer import write_to_sheet
from agents import graph_client
from utils.logger import get_logger

logger = get_logger(__name__)

_subscription_id: str | None = None
_RENEWAL_INTERVAL_SECONDS = 48 * 3600  # renova a cada 48h (subscription dura ~3 dias)


async def _renewal_loop() -> None:
    global _subscription_id
    while True:
        await asyncio.sleep(_RENEWAL_INTERVAL_SECONDS)
        if not _subscription_id:
            continue
        try:
            graph_client.renew_subscription(_subscription_id)
            logger.info("Subscription renovada automaticamente")
        except Exception as e:
            logger.error(f"Falha na renovação automática: {e} — tentando recriar subscription")
            base_url = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
            if base_url:
                try:
                    _subscription_id = graph_client.create_subscription(f"{base_url}/graph-webhook")
                    logger.info(f"Subscription recriada: {_subscription_id}")
                except Exception as e2:
                    logger.error(f"Falha ao recriar subscription: {e2}")


async def _create_subscription_deferred() -> None:
    """Aguarda o servidor estar pronto antes de registrar a subscription no Graph API.
    O Graph valida a notificationUrl durante o POST /subscriptions — se o servidor
    ainda não estiver aceitando requisições, a validação falha com 400."""
    global _subscription_id
    await asyncio.sleep(5)
    base_url = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
    if not base_url:
        logger.warning("WEBHOOK_BASE_URL não configurada — subscription não registrada")
        return
    try:
        _subscription_id = graph_client.create_subscription(f"{base_url}/graph-webhook")
    except Exception as e:
        logger.error(f"Falha ao criar subscription no Graph API: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_task = asyncio.create_task(_create_subscription_deferred())
    renewal_task = asyncio.create_task(_renewal_loop())
    yield
    startup_task.cancel()
    renewal_task.cancel()
    with suppress(asyncio.CancelledError):
        await startup_task
    with suppress(asyncio.CancelledError):
        await renewal_task


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "subscription_id": _subscription_id}


@app.get("/graph-webhook")
async def graph_webhook_validate_get(validationToken: str | None = None):
    if validationToken:
        return PlainTextResponse(content=validationToken, status_code=200)
    return PlainTextResponse(content="ok", status_code=200)


@app.post("/graph-webhook")
async def graph_webhook(request: Request, validationToken: str | None = None):
    """Recebe change notifications do Microsoft Graph.
    O Graph API envia a validação como POST com validationToken no query string."""
    if validationToken:
        return PlainTextResponse(content=validationToken, status_code=200)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "payload inválido"}, status_code=400)

    try:
        notification = GraphNotificationPayload(**body)
    except Exception as e:
        logger.error(f"Notificação inválida: {e}")
        return JSONResponse({"error": str(e)}, status_code=422)

    for item in notification.value:
        if item.changeType != "created":
            continue
        if not item.resourceData or not item.resourceData.id:
            logger.warning("Notificação sem resourceData.id — ignorada")
            continue
        _process_message(item.resourceData.id)

    # Graph exige 202 rápido
    return JSONResponse({}, status_code=202)


@app.post("/subscriptions/renew")
async def renew_subscription():
    """Renova manualmente a subscription antes de expirar (validade máx. ~3 dias)."""
    if not _subscription_id:
        return JSONResponse({"error": "Nenhuma subscription ativa"}, status_code=404)
    try:
        graph_client.renew_subscription(_subscription_id)
        return {"status": "renovada", "subscription_id": _subscription_id}
    except Exception as e:
        logger.error(f"Erro ao renovar subscription: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _process_message(message_id: str) -> None:
    try:
        msg = graph_client.get_message(message_id)
    except Exception as e:
        logger.error(f"Erro ao buscar email {message_id}: {e}")
        return

    from_addr = msg.get("from", {}).get("emailAddress", {})

    # Filtro 1: ignora emails enviados pelo remetente configurado
    ignore_from = os.environ.get("FILTER_IGNORE_FROM", "").lower()
    if ignore_from and from_addr.get("address", "").lower() == ignore_from:
        logger.info(f"Email de {from_addr.get('address')} ignorado (FILTER_IGNORE_FROM)")
        return

    # Filtro 2: processa apenas emails destinados ao endereço configurado
    target_to = os.environ.get("MAILBOX_TARGET_EMAIL", "").lower()
    if target_to:
        to_addresses = [
            r.get("emailAddress", {}).get("address", "").lower()
            for r in msg.get("toRecipients", [])
        ]
        if target_to not in to_addresses:
            logger.info(f"Email não endereçado a {target_to} — ignorado")
            return

    body_content = msg.get("body", {}).get("content", "")

    payload = EmailPayload.model_validate({
        "subject": msg.get("subject", ""),
        "body": body_content,
        "receivedDateTime": msg.get("receivedDateTime", ""),
        "from": from_addr.get("address", ""),
        "fromName": from_addr.get("name"),
        "messageId": msg.get("id", message_id),
        "conversationId": msg.get("conversationId", ""),
    })

    try:
        analysis = analyze_email(payload)
    except Exception as e:
        logger.error(f"Erro na análise Gemini para {message_id}: {e}")
        return

    logger.info(f"is_recusa={analysis.is_recusa}, confianca={analysis.confianca}")

    if not analysis.is_recusa:
        return

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
    except Exception as e:
        logger.error(f"Erro ao gravar no Sheets para {message_id}: {e}")
        return

    if tipo_interacao != "primeira":
        return

    nf = analysis.nota_fiscal or "não identificada"
    body_html = (
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

    try:
        graph_client.send_reply(payload.messageId, body_html)
    except Exception as e:
        logger.error(f"Erro ao enviar reply para {message_id}: {e}")
