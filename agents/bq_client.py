import os
import json
from google.cloud import bigquery
from google.oauth2.service_account import Credentials
from utils.logger import get_logger
from utils.retry import transient_retry

logger = get_logger(__name__)

_BQ_TIMEOUT_SECONDS = 30

_BQ_TABLE = "soma-dl-refined-online.atacado_processed.info_fat_nf"


def _get_client() -> bigquery.Client:
    creds_raw = os.environ.get("BQ_CREDENTIALS_JSON")
    if not creds_raw:
        raise ValueError("BQ_CREDENTIALS_JSON não configurada")
    creds_dict = json.loads(creds_raw)
    credentials = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(project=creds_dict.get("project_id"), credentials=credentials)


@transient_retry
def _run_bq_query(nota_fiscal: str, client: bigquery.Client) -> tuple[bool, int]:
    query = """
        SELECT 1
        FROM `soma-dl-refined-online.atacado_processed.info_fat_nf`
        WHERE NF_SAIDA = @nota_fiscal
        LIMIT 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("nota_fiscal", "STRING", nota_fiscal)
        ]
    )
    job = client.query(query, job_config=job_config)
    rows = list(job.result(timeout=_BQ_TIMEOUT_SECONDS))
    return len(rows) > 0, job.total_bytes_billed or 0


def is_nf_atacado(nota_fiscal: str) -> bool:
    client = _get_client()
    found, bytes_billed = _run_bq_query(nota_fiscal, client)
    logger.info(f"BigQuery — NF {nota_fiscal}: {'Atacado' if found else 'não encontrada (Varejo)'}")
    try:
        from utils import pricing, supabase_client
        event = pricing.bq_cost_brl(bytes_billed)
        supabase_client.insert_usage_event(origem="bigquery", **event)
    except Exception as e:
        logger.warning(f"Falha ao registrar uso BQ: {e}")
    return found
