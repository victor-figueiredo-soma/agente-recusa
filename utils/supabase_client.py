import os
from utils.logger import get_logger

logger = get_logger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        _client = create_client(url, key)
    return _client


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
    _get_client().table("token_usage").insert({
        "origem": origem,
        "tipo_token": tipo_token,
        "quantidade": quantidade,
        "unidade": unidade,
        "custo_brl": round(custo_brl, 6),
        "email_id": email_id,
    }).execute()
