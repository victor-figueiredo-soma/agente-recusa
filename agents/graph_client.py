import os
import requests
import msal
from utils.logger import get_logger
from utils.retry import transient_retry

logger = get_logger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_SCOPES = ["https://graph.microsoft.com/.default"]

_msal_app: msal.ConfidentialClientApplication | None = None


def _get_msal_app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            os.environ["AZURE_CLIENT_ID"],
            authority=f"https://login.microsoftonline.com/{os.environ['AZURE_TENANT_ID']}",
            client_credential=os.environ["AZURE_CLIENT_SECRET"],
        )
    return _msal_app


def _get_token() -> str:
    result = _get_msal_app().acquire_token_for_client(scopes=_SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"MSAL token error: {result.get('error_description', result)}")
    return result["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}", "Content-Type": "application/json"}


@transient_retry
def get_message(message_id: str) -> dict:
    user_id = os.environ["MAILBOX_USER_ID"]
    select = "id,conversationId,subject,body,from,toRecipients,ccRecipients,receivedDateTime"
    url = f"{_GRAPH_BASE}/users/{user_id}/messages/{message_id}?$select={select}"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


@transient_retry
def get_conversation_messages(
    conversation_id: str,
    exclude_id: str | None = None,
    top: int = 5,
) -> list[dict]:
    """Retorna as últimas `top` mensagens da thread, excluindo `exclude_id`."""
    user_id = os.environ["MAILBOX_USER_ID"]
    params = {
        "$filter": f"conversationId eq '{conversation_id}'",
        "$top": str(top + (1 if exclude_id else 0)),
        "$select": "id,subject,body,from,receivedDateTime",
    }
    url = f"{_GRAPH_BASE}/users/{user_id}/messages"
    resp = requests.get(url, headers=_headers(), params=params, timeout=15)
    resp.raise_for_status()
    messages = resp.json().get("value", [])
    if exclude_id:
        messages = [m for m in messages if m.get("id") != exclude_id]
    messages.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return messages[:top]


def _list_subscriptions() -> list[dict]:
    resp = requests.get(f"{_GRAPH_BASE}/subscriptions", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_subscription(subscription_id: str) -> dict | None:
    """Retorna a subscription do Graph, ou None se não existir mais (404).
    Usado pelo watchdog para detectar subscription ausente/expirada."""
    resp = requests.get(
        f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
        headers=_headers(),
        timeout=15,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _delete_subscription(subscription_id: str) -> None:
    resp = requests.delete(
        f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()


def cleanup_stale_subscriptions(notification_url: str) -> None:
    """Deleta subscriptions que apontam para caixas diferentes da atual."""
    user_id = os.environ["MAILBOX_USER_ID"]
    expected_resource = f"users/{user_id}/mailFolders/Inbox/messages"
    try:
        subs = _list_subscriptions()
    except Exception as e:
        logger.warning(f"Não foi possível listar subscriptions para limpeza: {e}")
        return
    for sub in subs:
        resource = sub.get("resource", "")
        sub_id = sub.get("id", "")
        if resource != expected_resource:
            try:
                _delete_subscription(sub_id)
                logger.info(f"Subscription obsoleta deletada: {sub_id} (resource: {resource})")
            except Exception as e:
                logger.warning(f"Falha ao deletar subscription obsoleta {sub_id}: {e}")


def create_subscription(notification_url: str) -> str:
    """Remove subscriptions obsoletas e cria nova para a caixa atual."""
    cleanup_stale_subscriptions(notification_url)
    user_id = os.environ["MAILBOX_USER_ID"]
    payload = {
        "changeType": "created",
        "notificationUrl": notification_url,
        "resource": f"users/{user_id}/mailFolders/Inbox/messages",
        "expirationDateTime": _expiration_datetime(),
        "clientState": os.environ["WEBHOOK_CLIENT_STATE"],
    }
    resp = requests.post(
        f"{_GRAPH_BASE}/subscriptions",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    if not resp.ok:
        logger.error(f"Graph API erro {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    sub_id = resp.json()["id"]
    logger.info(f"Subscription criada: {sub_id}")
    return sub_id


def renew_subscription(subscription_id: str) -> None:
    payload = {"expirationDateTime": _expiration_datetime()}
    resp = requests.patch(
        f"{_GRAPH_BASE}/subscriptions/{subscription_id}",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    logger.info(f"Subscription renovada: {subscription_id}")


@transient_retry
def send_reply(message_id: str, body_html: str) -> None:
    """Cria reply draft, substitui To: pelo email de notificação e envia."""
    user_id = os.environ["MAILBOX_USER_ID"]
    notification_email = os.environ["NOTIFICATION_EMAIL"]

    # 1. Criar draft de reply
    create_url = f"{_GRAPH_BASE}/users/{user_id}/messages/{message_id}/createReply"
    resp = requests.post(create_url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    draft_id = resp.json()["id"]

    # 2. Substituir destinatário e corpo
    patch_url = f"{_GRAPH_BASE}/users/{user_id}/messages/{draft_id}"
    patch_payload = {
        "toRecipients": [{"emailAddress": {"address": notification_email}}],
        "ccRecipients": [],
        "bccRecipients": [],
        "body": {"contentType": "HTML", "content": body_html},
    }
    resp = requests.patch(patch_url, json=patch_payload, headers=_headers(), timeout=15)
    resp.raise_for_status()

    # 3. Enviar
    send_url = f"{_GRAPH_BASE}/users/{user_id}/messages/{draft_id}/send"
    resp = requests.post(send_url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    logger.info(f"Reply enviado para {notification_email} (thread de {message_id})")


def send_alert_mail(to: str, subject: str, body_html: str) -> None:
    """Envia um e-mail de alerta standalone (não-reply) para `to`.
    Sem @transient_retry e sem logging interno de propósito: é chamado pelo
    handler de alerta de erros e não pode recursar nem disparar novos alertas."""
    user_id = os.environ["MAILBOX_USER_ID"]
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": False,
    }
    resp = requests.post(
        f"{_GRAPH_BASE}/users/{user_id}/sendMail",
        json=payload,
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()


def _expiration_datetime() -> str:
    from datetime import datetime, timezone, timedelta
    # Graph API permite máx. ~4230 min para mail subscriptions (~3 dias)
    expires = datetime.now(timezone.utc) + timedelta(days=2)
    return expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")
