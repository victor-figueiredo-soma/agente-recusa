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
    return False


transient_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
