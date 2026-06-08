import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception, before_sleep_log

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, TimeoutError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    try:
        import gspread.exceptions
        if isinstance(exc, gspread.exceptions.APIError):
            return exc.response.status_code in (429, 500, 503)
    except Exception:
        pass
    try:
        from google.api_core import exceptions as gexc
        if isinstance(exc, (gexc.ServiceUnavailable, gexc.InternalServerError, gexc.DeadlineExceeded)):
            return True
    except Exception:
        pass
    # google-genai (SDK do Gemini) levanta suas próprias exceções, não as do
    # google.api_core. APIError.code é o status HTTP — retry em 429 e 5xx.
    try:
        from google.genai import errors as genai_errors
        if isinstance(exc, genai_errors.APIError):
            return getattr(exc, "code", None) in (408, 429, 500, 502, 503, 504)
    except Exception:
        pass
    # google-genai usa httpx por baixo — erros de rede transitórios.
    try:
        import httpx
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
            return True
    except Exception:
        pass
    return False


transient_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
