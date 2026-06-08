import html
import logging
import os
import sys
import threading
import time
import traceback

_AGENT_NAME = "Agente Recusa"

# Não repete o e-mail do mesmo erro dentro dessa janela (anti-spam)
_ALERT_THROTTLE_SECONDS = 300

# Loggers de terceiros não disparam alerta (evita ruído e recursão via msal/requests)
_ALERT_EXCLUDED_PREFIXES = ("msal", "urllib3", "requests", "asyncio", "uvicorn")

_alert_state_lock = threading.Lock()
_alert_last_sent: dict[str, float] = {}
_alert_in_delivery = 0  # > 0 enquanto entregamos um alerta: suprime alertas aninhados
_alert_handler_installed = False


class EmailAlertHandler(logging.Handler):
    """Envia e-mail de alerta para ALERT_EMAIL em qualquer log de nível ERROR.

    Não-bloqueante (envio em thread daemon) e com throttle por mensagem, para
    que novos pontos de erro fiquem cobertos automaticamente, sem novo ajuste."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if _alert_in_delivery > 0:
                return
            if record.name.split(".")[0] in _ALERT_EXCLUDED_PREFIXES:
                return
            to = os.environ.get("ALERT_EMAIL", "").strip()
            if not to:
                return

            msg = record.getMessage()
            now = time.monotonic()
            with _alert_state_lock:
                last = _alert_last_sent.get(msg)
                if last is not None and (now - last) < _ALERT_THROTTLE_SECONDS:
                    return
                _alert_last_sent[msg] = now
                if len(_alert_last_sent) > 200:  # poda para não crescer indefinidamente
                    stale = [k for k, t in _alert_last_sent.items() if now - t > _ALERT_THROTTLE_SECONDS]
                    for k in stale:
                        _alert_last_sent.pop(k, None)

            threading.Thread(
                target=_deliver_alert, args=(to, record, msg), daemon=True
            ).start()
        except Exception:
            # Alertar nunca pode derrubar o fluxo principal nem recursar em log
            pass


def _deliver_alert(to: str, record: logging.LogRecord, msg: str) -> None:
    global _alert_in_delivery
    with _alert_state_lock:
        _alert_in_delivery += 1
    try:
        from agents import graph_client  # import tardio: evita ciclo de import

        detalhe = msg
        if record.exc_info:
            detalhe += "\n\n" + "".join(traceback.format_exception(*record.exc_info))

        ts = time.strftime("%d/%m/%Y %H:%M:%S", time.gmtime())
        subject = f"[{_AGENT_NAME}] Erro: {msg[:120]}"
        body_html = (
            f"<p>O <strong>{_AGENT_NAME}</strong> registrou um erro.</p>"
            f"<ul>"
            f"<li><strong>Origem:</strong> {html.escape(record.name)}</li>"
            f"<li><strong>Nível:</strong> {record.levelname}</li>"
            f"<li><strong>Horário (UTC):</strong> {ts}</li>"
            f"</ul>"
            f"<p><strong>Motivo:</strong></p>"
            f"<pre style=\"white-space:pre-wrap;font-family:monospace\">{html.escape(detalhe)}</pre>"
        )
        graph_client.send_alert_mail(to, subject, body_html)
    except Exception:
        # Falha ao alertar é silenciosa de propósito (não logar → não recursar)
        pass
    finally:
        with _alert_state_lock:
            _alert_in_delivery -= 1


def get_logger(name: str) -> logging.Logger:
    global _alert_handler_installed
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S"
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

    # Handler de alerta instalado uma única vez no root: captura os ERROR de
    # todos os loggers por propagação, atuais e futuros.
    if not _alert_handler_installed:
        logging.getLogger().addHandler(EmailAlertHandler(level=logging.ERROR))
        _alert_handler_installed = True

    return logger
