import os
import json
from datetime import datetime, timezone
import gspread
from google.oauth2.service_account import Credentials
from models.schemas import ProcessedEmail
from utils.logger import get_logger

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
    "Assunto",
    "Nota Fiscal",
    "Motivo da Recusa",
    "Confiança",
    "Tipo de Interação",
    "Ação",
]


def _get_client() -> gspread.Client:
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_raw:
        raise ValueError("GOOGLE_CREDENTIALS_JSON não configurada")

    creds_dict = json.loads(creds_raw)
    creds = Credentials.from_service_account_info(creds_dict, scopes=_SCOPES)
    return gspread.authorize(creds)


def _ensure_headers(worksheet: gspread.Worksheet) -> None:
    existing = worksheet.row_values(1)
    if existing != _HEADERS:
        worksheet.update("A1", [_HEADERS])
        worksheet.format("A1:J1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.6},
        })


def _check_interaction_type(worksheet: gspread.Worksheet, nota_fiscal: str | None, conversation_id: str) -> str:
    """
    Retorna:
    - "reinteracao_mesma_thread"  → mesmo conversationId já registrado
    - "reinteracao_nova_thread"   → mesma NF, thread diferente
    - "primeira"                  → NF e thread nunca vistos antes
    """
    records = worksheet.get_all_records()

    for r in records:
        if r.get("Id da Conversa") == conversation_id:
            return "reinteracao_mesma_thread"

    if nota_fiscal:
        for r in records:
            if r.get("Nota Fiscal") == nota_fiscal:
                return "reinteracao_nova_thread"

    return "primeira"


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

    tipo_interacao = _check_interaction_type(worksheet, record.nota_fiscal, record.conversation_id)
    record.tipo_interacao = tipo_interacao

    row = [
        record.message_id,
        record.conversation_id,
        record.data_hora_recebimento,
        record.remetente,
        record.assunto,
        record.nota_fiscal or "",
        record.motivo_recusa or "",
        record.confianca or "",
        tipo_interacao,
        record.acao,
    ]

    worksheet.append_row(row, value_input_option="USER_ENTERED")
    logger.info(f"Linha adicionada ao Sheets: '{record.assunto}' — {tipo_interacao}")
    return tipo_interacao
