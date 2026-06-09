import asyncio
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from models.schemas import EmailPayload, ProcessedEmail, GraphNotificationPayload
from agents.email_analyzer import analyze_email
from agents.sheet_writer import write_to_sheet
from agents import graph_client, bq_client
from utils.logger import get_logger

logger = get_logger(__name__)

_SP_TZ = ZoneInfo("America/Sao_Paulo")

_subscription_id: str | None = None
_in_flight: set[str] = set()  # message_ids atualmente em processamento


def _fmt_sp_time(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.astimezone(_SP_TZ).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return dt_str


# Renova bem antes da expiração (subscription dura 2 dias) para dar folga a
# restarts/drift — renovar no instante exato da expiração causa PATCH 400.
_RENEWAL_INTERVAL_SECONDS = 24 * 3600  # renova a cada 24h


async def _renewal_loop() -> None:
    global _subscription_id
    while True:
        await asyncio.sleep(_RENEWAL_INTERVAL_SECONDS)
        if not _subscription_id:
            continue
        try:
            # to_thread: o renew/create usa requests síncrono; rodar direto na
            # corrotina trava o event loop e impede o FastAPI de responder a
            # requisição de validação do Graph dentro dos 10s → "validation timed out".
            await asyncio.to_thread(graph_client.renew_subscription, _subscription_id)
            logger.info("Subscription renovada automaticamente")
        except Exception as e:
            logger.error(f"Falha na renovação automática: {e} — tentando recriar subscription")
            base_url = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
            if base_url:
                try:
                    _subscription_id = await asyncio.to_thread(
                        graph_client.create_subscription, f"{base_url}/graph-webhook"
                    )
                    logger.info(f"Subscription recriada: {_subscription_id}")
                except Exception as e2:
                    logger.error(f"Falha ao recriar subscription: {e2}")


_WATCHDOG_INTERVAL_SECONDS = 3600  # verifica a saúde da subscription a cada 1h


async def _watchdog_loop() -> None:
    """Auto-recuperação: garante que sempre exista uma subscription válida.

    Independente do timer de renovação, verifica periodicamente se a subscription
    ainda existe no Graph e a recria sozinho caso tenha sumido/expirado ou nunca
    tenha sido criada (ex.: falha de validação no startup). Robusto a restart do
    Railway e a falhas de renovação — sem necessidade de novo deploy/ajuste."""
    global _subscription_id
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)
        base_url = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
        if not base_url:
            continue
        try:
            needs_recreate = True
            if _subscription_id:
                sub = await asyncio.to_thread(graph_client.get_subscription, _subscription_id)
                needs_recreate = sub is None
            if needs_recreate:
                logger.warning("Watchdog: subscription ausente/expirada — recriando")
                _subscription_id = await asyncio.to_thread(
                    graph_client.create_subscription, f"{base_url}/graph-webhook"
                )
                logger.info(f"Watchdog: subscription recriada: {_subscription_id}")
        except Exception as e:
            logger.error(f"Watchdog: falha ao verificar/recriar subscription: {e}")


async def _create_subscription_deferred() -> None:
    """Aguarda o servidor estar pronto antes de registrar a subscription no Graph API.
    O Graph valida a notificationUrl durante o POST /subscriptions — se o servidor
    ainda não estiver aceitando requisições, a validação falha com 400."""
    global _subscription_id
    
    # Mantemos uma folga segura para o Railway atualizar os IPs de borda
    await asyncio.sleep(10) 
    
    base_url = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
    if not base_url:
        logger.warning("WEBHOOK_BASE_URL não configurada — subscription não registrada")
        return

    try:
        # --- AQUI ESTÁ A MUDANÇA CRUCIAL ---
        # asyncio.to_thread joga o 'requests.post' síncrono para outra thread.
        # Isso impede que o loop do FastAPI trave e permite que ele responda 
        # o 'validationToken' imediatamente no milissegundo em que a Microsoft chamar.
        _subscription_id = await asyncio.to_thread(
            graph_client.create_subscription, f"{base_url}/graph-webhook"
        )
    except Exception as e:
        logger.error(f"Falha ao criar subscription no Graph API: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(_create_subscription_deferred()),
        asyncio.create_task(_renewal_loop()),
        asyncio.create_task(_watchdog_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError):
            await task


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

    expected_state = os.environ.get("WEBHOOK_CLIENT_STATE", "")

    for item in notification.value:
        if item.clientState != expected_state:
            logger.warning("Notificação com clientState inválido — descartada")
            continue
        if item.changeType != "created":
            continue
        if not item.resourceData or not item.resourceData.id:
            logger.warning("Notificação sem resourceData.id — ignorada")
            continue
        msg_id = item.resourceData.id
        if msg_id in _in_flight:
            logger.info(f"Mensagem {msg_id} já em processamento — notificação duplicada ignorada")
            continue
        _in_flight.add(msg_id)
        asyncio.create_task(_dispatch_message(msg_id))

    # 202 imediato — Graph resenvia se não receber resposta em ~3s
    return JSONResponse({}, status_code=202)


@app.post("/subscriptions/renew")
async def renew_subscription(x_api_key: str | None = Header(default=None)):
    """Renova manualmente a subscription antes de expirar (validade máx. ~3 dias)."""
    expected = os.environ.get("RENEW_API_KEY", "")
    if not expected or x_api_key != expected:
        return JSONResponse({"error": "Não autorizado"}, status_code=401)
    if not _subscription_id:
        return JSONResponse({"error": "Nenhuma subscription ativa"}, status_code=404)
    try:
        graph_client.renew_subscription(_subscription_id)
        return {"status": "renovada", "subscription_id": _subscription_id}
    except Exception as e:
        logger.error(f"Erro ao renovar subscription: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def _dispatch_message(message_id: str) -> None:
    """Executa _process_message em thread separada e libera _in_flight ao final."""
    try:
        await asyncio.to_thread(_process_message, message_id)
    finally:
        _in_flight.discard(message_id)


def _process_message(message_id: str) -> None:
    start_time = time.monotonic()
    # Preenchido pelo inner assim que a thread é conhecida (após os filtros). Permite
    # contabilizar o Railway com o id da THREAD; cai no message_id apenas se o inner
    # sair antes de identificar a thread (falha ao buscar a mensagem ou e-mail filtrado),
    # casos em que não queremos marcar nenhuma thread como processada.
    ctx: dict[str, str | None] = {"thread_id": None}
    try:
        _process_message_inner(message_id, ctx)
    finally:
        try:
            from utils import pricing
            event = pricing.railway_cost_brl(time.monotonic() - start_time)
            bq_client.insert_usage_event(
                origem="railway",
                thread_id=ctx["thread_id"] or message_id,
                **event,
            )
        except Exception as e:
            logger.warning(f"Falha ao registrar custo Railway: {e}")


def _process_message_inner(message_id: str, ctx: dict | None = None) -> None:
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
            for r in msg.get("toRecipients", []) + msg.get("ccRecipients", [])
        ]
        logger.info(f"Destinatários (To+Cc): {to_addresses}")
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

    if ctx is not None:
        ctx["thread_id"] = payload.conversationId

    # Idempotência por THREAD: olhamos cada conversa uma única vez. Se o id da thread
    # já está na tabela de custos, ela já foi processada — pula, evitando custo de
    # Gemini/BQ duplicado e chamado repetido por reenvio/continuidade na mesma thread.
    # Fail-open: se a checagem falhar, processa mesmo assim.
    try:
        if bq_client.thread_already_processed(payload.conversationId):
            logger.info(f"Thread {payload.conversationId} já processada — email {payload.messageId} ignorado")
            return
    except Exception as e:
        logger.warning(f"Falha ao checar idempotência da thread {payload.conversationId}: {e} — processando mesmo assim")

    try:
        thread_history = graph_client.get_conversation_messages(
            payload.conversationId, exclude_id=payload.messageId
        )
        logger.info(f"Histórico da thread {payload.conversationId}: {len(thread_history)} mensagem(ns) encontrada(s)")
    except Exception as e:
        logger.warning(f"Não foi possível buscar histórico da thread {payload.conversationId}: {e}")
        thread_history = None

    try:
        analysis = analyze_email(payload, thread_history=thread_history)
    except Exception as e:
        logger.error(f"Erro na análise Gemini para {message_id}: {e}")
        return

    logger.info(f"is_recusa={analysis.is_recusa}, confianca={analysis.confianca}")

    if not analysis.is_recusa:
        return

    if not analysis.nota_fiscal:
        logger.info(f"Email {message_id} classificado como recusa mas sem NF identificada — ignorado")
        return

    remetente = f"{payload.fromName} <{payload.from_email}>" if payload.fromName else payload.from_email

    nfs_raw = analysis.nota_fiscal
    nfs = [nf.strip() for nf in nfs_raw.split(",") if nf.strip()]

    novos_chamados: list[str] = []
    ja_registradas: list[str] = []

    for nf in nfs:
        if nf:
            try:
                if not bq_client.is_nf_atacado(nf, thread_id=payload.conversationId):
                    logger.info(f"NF {nf} não é do Atacado — ignorada")
                    continue
            except Exception as e:
                logger.error(f"Erro ao validar NF {nf} no BigQuery: {e} — processando mesmo assim")

        record = ProcessedEmail(
            message_id=payload.messageId,
            conversation_id=payload.conversationId,
            data_hora_recebimento=payload.receivedDateTime,
            remetente=remetente,
            transportadora=analysis.transportadora,
            tipo_mensagem=analysis.tipo_mensagem,
            assunto=payload.subject,
            nota_fiscal=nf,
            motivo_recusa=analysis.motivo_recusa,
            confianca=analysis.confianca,
            status=analysis.status or "RECUSA",
        )
        try:
            tipo_interacao = write_to_sheet(record)
        except Exception as e:
            logger.error(f"Erro ao gravar no Sheets para NF {nf} ({message_id}): {e}")
            continue

        nf_label = nf or "não identificada"

        # Garante o chamado no BQ INDEPENDENTEMENTE do tipo de interação do Sheets.
        # O insert_chamado_if_absent dedup por NF no próprio BQ: se a NF já existe é
        # no-op; se está faltando (ex.: chamado já criado no Sheets mas ausente do BQ,
        # caso de registros anteriores à migração), é inserido mesmo assim. A gravação
        # no BQ não pode ficar refém da classificação do Sheets.
        # SITUACAO reflete se a NF já constava no Sheets: "primeira" → recém-criada;
        # qualquer reiteração → a NF já existia no Sheets ("Chamado já existente").
        situacao = "Chamado Criado" if tipo_interacao == "primeira" else "Chamado já existente"
        try:
            bq_client.insert_chamado_if_absent(
                status=analysis.status or "RECUSA",
                sub_motivo=analysis.sub_motivo or "PENDENTE",
                nota_fiscal=nf,
                thread_id=payload.conversationId,
                situacao=situacao,
            )
        except Exception as e:
            # ERROR (não warning): falha de gravação do chamado é perda de dado
            # operacional — precisa ser visível para alerta/monitoramento, não mascarada.
            logger.error(
                f"Falha ao registrar chamado no BigQuery — NF {nf}, "
                f"msg {message_id}, conversa {payload.conversationId}: {e}",
                exc_info=True,
            )

        # Categorização da notificação à logística (separada da gravação no BQ).
        if tipo_interacao == "primeira":
            novos_chamados.append(nf_label)
        elif tipo_interacao == "reiteracao_outra_thread":
            ja_registradas.append(nf_label)
        # reiteracao_mesma_thread → ignora silenciosamente

    if not novos_chamados and not ja_registradas:
        return

    transportadora = (analysis.transportadora or "não identificada").upper()
    motivo = analysis.motivo_recusa or "não identificado"
    data_sp = _fmt_sp_time(payload.receivedDateTime)
    _status = analysis.status or "RECUSA"
    is_retencao = _status == "RETENÇÃO FISCAL"
    is_extravio = _status == "EXTRAVIO"

    if novos_chamados and ja_registradas:
        qtd = len(novos_chamados)
        ja_qtd = len(ja_registradas)
        if is_retencao:
            header_text = f"{qtd} retenção(ões) fiscal(is) registrada(s). {ja_qtd} NF(s) já possuíam chamado aberto."
        elif is_extravio:
            header_text = f"{qtd} extravio(s) registrado(s). {ja_qtd} NF(s) já possuíam chamado aberto."
        else:
            header_text = f"{qtd} recusa(s) registrada(s). {ja_qtd} NF(s) já possuíam chamado aberto."
        novos_list = "".join(f"<li>Chamado {i + 1} — NF {nf}</li>" for i, nf in enumerate(novos_chamados))
        ja_list = "".join(f"<li>NF {nf} — chamado já existente no Sheets</li>" for nf in ja_registradas)
        nf_items = (
            f"<li><strong>Novos chamados:</strong><ul>{novos_list}</ul></li>"
            f"<li><strong>Chamados já existentes:</strong><ul>{ja_list}</ul></li>"
        )
    elif novos_chamados:
        qtd = len(novos_chamados)
        if qtd == 1:
            if is_retencao:
                header_text = "Retenção fiscal registrada."
            elif is_extravio:
                header_text = "Extravio registrado."
            else:
                header_text = "Recusa registrada."
            nf_items = f"<li><strong>Nota Fiscal:</strong> {novos_chamados[0]}</li>"
        else:
            if is_retencao:
                header_text = f"{qtd} retenções fiscais registradas."
            elif is_extravio:
                header_text = f"{qtd} extravios registrados."
            else:
                header_text = f"{qtd} recusas registradas."
            nf_list = "".join(f"<li>Chamado {i + 1} — NF {nf}</li>" for i, nf in enumerate(novos_chamados))
            nf_items = f"<li><strong>Notas Fiscais ({qtd} chamados):</strong><ul>{nf_list}</ul></li>"
    else:
        header_text = "Chamado(s) já aberto(s) para a(s) NF(s) informada(s) — nenhum novo registro criado."
        ja_list = "".join(f"<li>NF {nf} — chamado já existente no Sheets</li>" for nf in ja_registradas)
        nf_items = f"<li><strong>Notas Fiscais:</strong><ul>{ja_list}</ul></li>"

    body_html = (
        f"<p>{header_text}</p>"
        f"<ul>"
        f"{nf_items}"
        f"<li><strong>Transportadora:</strong> {transportadora}</li>"
        f"<li><strong>Motivo:</strong> {motivo}</li>"
        f"<li><strong>Data de recebimento:</strong> {data_sp}</li>"
        f"</ul>"
        f"<p>Acesse o Sheets para visualizar o(s) registro(s).</p>"
    )

    try:
        graph_client.send_reply(payload.messageId, body_html)
    except Exception as e:
        logger.error(f"Erro ao enviar reply para {message_id}: {e}")
