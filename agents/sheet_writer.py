import os
import json
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials
from models.schemas import ProcessedEmail
from utils.logger import get_logger
from utils.retry import transient_retry

_SP_TZ = ZoneInfo("America/Sao_Paulo")

_gs_client: gspread.Client | None = None
_gs_lock = threading.Lock()


def _to_sp_time(dt_str: str) -> str:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.astimezone(_SP_TZ).strftime("%d/%m/%Y %H:%M")
    except Exception:
        return dt_str

logger = get_logger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_HEADERS = [
    "Id da Mensagem",
    "Id da Conversa",
    "Data/Hora Recebimento",
    "Remetente",
    "Transportadora",
    "Assunto",
    "Nota Fiscal",
    "Status",
    "Motivo da Recusa",
    "Confiança",
    "Tipo de Interação",
    "Ação",
]


def _get_client() -> gspread.Client:
    """Cliente gspread em cache (singleton). Evita refazer a troca OAuth
    (round-trip de rede) e o parse de credenciais a cada gravação."""
    global _gs_client
    if _gs_client is None:
        with _gs_lock:
            if _gs_client is None:
                creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
                if not creds_raw:
                    raise ValueError("GOOGLE_CREDENTIALS_JSON não configurada")
                creds_dict = json.loads(creds_raw)
                creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
                _gs_client = gspread.authorize(creds)
    return _gs_client


def _ensure_headers(worksheet: gspread.Worksheet) -> None:
    existing = worksheet.row_values(1)
    if existing != _HEADERS:
        worksheet.update("A1", [_HEADERS])
        worksheet.format("A1:L1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.6},
        })


def _nf_ja_registrada(
    worksheet: gspread.Worksheet,
    nota_fiscal: str | None,
    conversation_id: str,
) -> str | None:
    """Retorna 'mesma_thread', 'outra_thread' ou None."""
    if not nota_fiscal:
        return None
    for r in worksheet.get_all_records():
        if str(r.get("Nota Fiscal", "")) == nota_fiscal:
            if str(r.get("Id da Conversa", "")) == conversation_id:
                return "mesma_thread"
            return "outra_thread"
    return None


@transient_retry
def write_to_sheet(record: ProcessedEmail) -> str:
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    worksheet_name = os.environ.get("WORKSHEET_NAME", "Emails Analisados")

    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_ID não configurada")

    client = _get_client()
    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=1000, cols=len(_HEADERS))
        logger.info(f"Aba '{worksheet_name}' criada")

    _ensure_headers(worksheet)

    reiteracao = _nf_ja_registrada(worksheet, record.nota_fiscal, record.conversation_id)
    if reiteracao:
        logger.info(f"NF {record.nota_fiscal} já registrada ({reiteracao}) — ignorada")
        record.tipo_interacao = "reinteracao"
        return f"reiteracao_{reiteracao}"

    tipo_interacao = "primeira"
    record.tipo_interacao = tipo_interacao

    row = [
        record.message_id,
        record.conversation_id,
        _to_sp_time(record.data_hora_recebimento),
        record.remetente,
        (record.transportadora or "").upper(),
        record.assunto,
        record.nota_fiscal or "",
        record.status,
        record.motivo_recusa or "",
        record.confianca or "",
        tipo_interacao,
        record.acao,
    ]

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Linha adicionada ao Sheets: '{record.assunto}' — {tipo_interacao}")
    return tipo_interacao
