import os
import json
import threading
from datetime import datetime, timezone
from google.cloud import bigquery
from google.oauth2.service_account import Credentials
from utils.logger import get_logger
from utils.retry import transient_retry

logger = get_logger(__name__)

_bq_client: bigquery.Client | None = None
_bq_lock = threading.Lock()

_BQ_TIMEOUT_SECONDS = 30

# Tabela de leitura — valida se a NF é do Atacado
_BQ_TABLE = "soma-dl-refined-online.atacado_processed.info_fat_nf"

# Tabelas de escrita do agente (substituem o Supabase)
_STORE_DATASET = "soma-dl-refined-online.control"
_CHAMADOS_TABLE = f"{_STORE_DATASET}.atacado_chamados_agente_recusa"
_CUSTOS_TABLE = f"{_STORE_DATASET}.atacado_custos_agente_recusa"


def _get_client() -> bigquery.Client:
    """Cliente BigQuery em cache (singleton). Evita reparsear credenciais e
    remontar a chave do service account a cada chamada — o client é seguro para
    uso concorrente entre threads."""
    global _bq_client
    if _bq_client is None:
        with _bq_lock:
            if _bq_client is None:
                creds_raw = os.environ.get("BQ_CREDENTIALS_JSON")
                if not creds_raw:
                    raise ValueError("BQ_CREDENTIALS_JSON não configurada")
                creds_dict = json.loads(creds_raw)
                credentials = Credentials.from_service_account_info(
                    creds_dict,
                    scopes=["https://www.googleapis.com/auth/bigquery"],
                )
                _bq_client = bigquery.Client(
                    project=creds_dict.get("project_id"), credentials=credentials
                )
    return _bq_client


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


def is_nf_atacado(nota_fiscal: str, thread_id: str | None = None) -> bool:
    client = _get_client()
    found, bytes_billed = _run_bq_query(nota_fiscal, client)
    logger.info(f"BigQuery — NF {nota_fiscal}: {'Atacado' if found else 'não encontrada (Varejo)'}")
    try:
        from utils import pricing
        event = pricing.bq_cost_brl(bytes_billed)
        insert_usage_event(origem="bigquery", thread_id=thread_id, **event)
    except Exception as e:
        logger.warning(f"Falha ao registrar uso BQ: {e}")
    return found


@transient_retry
def insert_chamado_if_absent(
    status: str,
    sub_motivo: str | None,
    nota_fiscal: str | None,
    thread_id: str | None = None,
) -> bool:
    """Insere o chamado apenas se a NF ainda não existir na tabela (dedup por NF).

    Unicidade: uma linha por NF — uma NF gera no máximo um chamado. A coluna MOTIVO
    armazena o sub-motivo PADRONIZADO (ver SUBMOTIVOS em models/schemas.py) e a coluna
    STATUS a categoria-pai, mas eles NÃO entram na chave de dedup.

    Usa um único DML atômico (INSERT ... SELECT ... WHERE NOT EXISTS), que evita o atraso
    do streaming buffer e resolve dedup + gravação em uma só query.

    Retorna True se inseriu (Chamado Criado) ou False se a NF já existia (Chamado já Criado).
    """
    client = _get_client()
    query = f"""
        INSERT INTO `{_CHAMADOS_TABLE}`
            (DATA, ID_EMAIL, NF, STATUS, MOTIVO, SITUACAO)
        SELECT CURRENT_TIMESTAMP(), @thread_id, @nf, @status, @motivo, 'Chamado Criado'
        FROM (SELECT 1)
        WHERE NOT EXISTS (
            SELECT 1 FROM `{_CHAMADOS_TABLE}` WHERE NF = @nf
        )
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("thread_id", "STRING", thread_id),
            bigquery.ScalarQueryParameter("nf", "STRING", nota_fiscal),
            bigquery.ScalarQueryParameter("status", "STRING", status),
            bigquery.ScalarQueryParameter("motivo", "STRING", sub_motivo),
        ]
    )
    job = client.query(query, job_config=job_config)
    job.result(timeout=_BQ_TIMEOUT_SECONDS)
    inserido = (job.num_dml_affected_rows or 0) > 0
    logger.info(
        f"BigQuery chamados — NF {nota_fiscal} / {status} / {sub_motivo}: "
        f"{'Chamado Criado' if inserido else 'Chamado já Criado'}"
    )
    return inserido


@transient_retry
def thread_already_processed(thread_id: str) -> bool:
    """True se a THREAD já foi vista antes — pela presença do seu id na tabela de
    custos. Olhamos cada conversa UMA única vez: o primeiro e-mail de uma thread a
    marca (os eventos de custo da execução são gravados com o id da thread), e os
    e-mails seguintes da mesma conversa são pulados.

    O custo deste scan é CONTABILIZADO (origem="bigquery"), inclusive quando a thread
    já existe — a query é cobrada de qualquer forma.

    Consequência: como qualquer evento de custo marca a thread, uma análise que
    falhe no primeiro e-mail ainda assim deixa a thread marcada (não reprocessada).
    Latência do streaming buffer: duplicatas da mesma thread em poucos segundos podem
    escapar; a dedup de chamados por NF é a trava final.
    """
    if not thread_id:
        return False
    client = _get_client()
    query = f"SELECT 1 FROM `{_CUSTOS_TABLE}` WHERE ID_EMAIL = @thread_id LIMIT 1"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("thread_id", "STRING", thread_id)]
    )
    job = client.query(query, job_config=job_config)
    rows = list(job.result(timeout=_BQ_TIMEOUT_SECONDS))
    # Contabiliza o custo do scan de dedup (cobrado mesmo quando a thread já existe).
    try:
        from utils import pricing
        event = pricing.bq_cost_brl(job.total_bytes_billed or 0)
        insert_usage_event(origem="bigquery", thread_id=thread_id, **event)
    except Exception as e:
        logger.warning(f"Falha ao registrar custo do scan de dedup: {e}")
    return len(rows) > 0


def insert_usage_event(
    origem: str,
    tipo_token: str,
    quantidade: float,
    unidade: str,
    custo_brl: float,
    thread_id: str | None = None,
) -> None:
    """Registra um evento de custo na tabela de custos (append-only via streaming insert).

    O identificador gravado é o id da THREAD (conversationId), não o do e-mail — uma
    conversa é a unidade de processamento. A coluna física continua se chamando
    ID_EMAIL (sem DDL), mas passa a conter o id da thread.

    Fire-and-forget: a linha é montada de forma síncrona (barato) e o insert roda
    em thread daemon, para não somar latência ao processamento do e-mail. Observa-
    bilidade não pode atrasar o caminho crítico."""
    if not os.environ.get("BQ_CREDENTIALS_JSON"):
        return
    row = {
        "DATA": datetime.now(timezone.utc).isoformat(),
        "ID_EMAIL": thread_id,
        "ORIGEM": origem,
        "TIPO_TOKEN": tipo_token,
        "QUANTIDADE": quantidade,
        "UNIDADE": unidade,
        "CUSTO": round(custo_brl, 6),
    }

    def _do_insert() -> None:
        try:
            errors = _get_client().insert_rows_json(_CUSTOS_TABLE, [row])
            if errors:
                logger.warning(f"Falha ao inserir evento de custo no BigQuery: {errors}")
        except Exception as e:
            logger.warning(f"Falha ao inserir evento de custo no BigQuery: {e}")

    threading.Thread(target=_do_insert, daemon=True).start()
