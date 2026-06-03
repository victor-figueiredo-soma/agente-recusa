import os


def _usd_to_brl() -> float:
    return float(os.environ.get("USD_TO_BRL", "5.30"))


def gemini_cost_brl(prompt_tokens: int, thinking_tokens: int, output_tokens: int) -> list[dict]:
    rate = _usd_to_brl()
    price_input = float(os.environ.get("GEMINI_PRICE_INPUT_PER_1M", "0.15"))
    price_thinking = float(os.environ.get("GEMINI_PRICE_THINKING_PER_1M", "3.50"))
    price_output = float(os.environ.get("GEMINI_PRICE_OUTPUT_PER_1M", "0.60"))

    events = []
    if prompt_tokens > 0:
        events.append({
            "tipo_token": "entrada",
            "quantidade": prompt_tokens,
            "unidade": "tokens",
            "custo_brl": (prompt_tokens / 1_000_000) * price_input * rate,
        })
    if thinking_tokens > 0:
        events.append({
            "tipo_token": "pensamento",
            "quantidade": thinking_tokens,
            "unidade": "tokens",
            "custo_brl": (thinking_tokens / 1_000_000) * price_thinking * rate,
        })
    if output_tokens > 0:
        events.append({
            "tipo_token": "saida",
            "quantidade": output_tokens,
            "unidade": "tokens",
            "custo_brl": (output_tokens / 1_000_000) * price_output * rate,
        })
    return events


def bq_cost_brl(bytes_billed: int) -> dict:
    rate = _usd_to_brl()
    price_per_tb = float(os.environ.get("BQ_PRICE_PER_TB", "6.25"))
    # BigQuery usa TB decimal (10^12 bytes) para cobrança
    gb = bytes_billed / 1e9
    cost_usd = (bytes_billed / 1e12) * price_per_tb
    return {
        "tipo_token": "processamento",
        "quantidade": round(gb, 6),
        "unidade": "gb",
        "custo_brl": cost_usd * rate,
    }


def railway_cost_brl(duration_seconds: float) -> dict:
    rate = _usd_to_brl()
    vcpu = float(os.environ.get("RAILWAY_VCPU", "1"))
    memory_gb = float(os.environ.get("RAILWAY_MEMORY_GB", "0.5"))
    # Railway: $0.000463/vCPU/min + $0.0000018/GB-RAM/min
    cost_usd = duration_seconds * (vcpu * 0.000463 / 60 + memory_gb * 0.0000018 / 60)
    return {
        "tipo_token": "processamento",
        "quantidade": round(duration_seconds, 3),
        "unidade": "segundos",
        "custo_brl": cost_usd * rate,
    }
