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
        scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
    )
    return bigquery.Client(project=creds_dict.get("project_id"), credentials=credentials)


@transient_retry
def is_nf_atacado(nota_fiscal: str) -> bool:
    """Retorna True se a NF existir na tabela de Atacado no BigQuery."""
    client = _get_client()
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
    rows = list(client.query(query, job_config=job_config).result(timeout=_BQ_TIMEOUT_SECONDS))
    found = len(rows) > 0
    logger.info(f"BigQuery — NF {nota_fiscal}: {'Atacado' if found else 'não encontrada (Varejo)'}")
    return found
