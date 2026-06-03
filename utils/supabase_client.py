import os
from datetime import datetime
from zoneinfo import ZoneInfo
from utils.logger import get_logger

logger = get_logger(__name__)

_SP_TZ = ZoneInfo("America/Sao_Paulo")
_client = None


def _get_client():
    global _client
    if _client is None:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        _client = create_client(url, key)
    return _client


def _now_sp() -> str:
    return datetime.now(_SP_TZ).isoformat()


def insert_usage_event(
    origem: str,
    tipo_token: str,
    quantidade: float,
    unidade: str,
    custo_brl: float,
    email_id: str | None = None,
) -> None:
    if not os.environ.get("SUPABASE_URL"):
        return
    _get_client().table("custos_agente_recusa").insert({
        "criado_em": _now_sp(),
        "origem": origem,
        "tipo_token": tipo_token,
        "quantidade": quantidade,
        "unidade": unidade,
        "custo_brl": round(custo_brl, 6),
        "email_id": email_id,
    }).execute()


def insert_chamado(
    status: str,
    motivo: str | None,
    nota_fiscal: str | None = None,
    email_id: str | None = None,
) -> None:
    if not os.environ.get("SUPABASE_URL"):
        return
    _get_client().table("chamados_agente_recusa").insert({
        "criado_em": _now_sp(),
        "status": status,
        "motivo": motivo,
        "nota_fiscal": nota_fiscal,
        "email_id": email_id,
    }).execute()
