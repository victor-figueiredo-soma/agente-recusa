"""Validação de NF por (número, série).

Trava a regressão do incidente de 28/09/2026: comunicados de NFs série 4 da
AZZAS 2154 tinham números iguais aos de NFs série 72 de 2025, e a validação só
por número abriu chamados e BDs para as notas antigas, de outros clientes.
Nenhuma chamada real ao BigQuery.
"""

from datetime import date, datetime

import pytest

from agents import bq_client
from models.schemas import AnalysisResult, separar_nf_serie

HOJE = date(2026, 10, 7)


# --- extração e parsing ------------------------------------------------------

@pytest.mark.parametrize("entrada, esperado", [
    ("1122836/4", "1122836/4"),
    ("1122836 / 4", "1122836/4"),
    ("1122836", "1122836"),
    ("1119516/4, 1119519/4, 1119520/4", "1119516/4, 1119519/4, 1119520/4"),
    ("119516/4", None),       # 6 dígitos não é NF
    ("12360337/890", None),   # 8 dígitos não é NF
    ("1528101/72, 123", "1528101/72"),
])
def test_validador_preserva_serie(entrada, esperado):
    assert AnalysisResult(is_recusa=True, nota_fiscal=entrada).nota_fiscal == esperado


@pytest.mark.parametrize("token, esperado", [
    ("1122836/4", ("1122836", "4")),
    ("1122836", ("1122836", None)),
    ("1528101/072", ("1528101", "72")),   # zero à esquerda some, como no faturamento
    ("1528101/0", ("1528101", "0")),
])
def test_separar_nf_serie(token, esperado):
    assert separar_nf_serie(token) == esperado


# --- validar_nf_atacado ------------------------------------------------------

@pytest.fixture
def consulta(monkeypatch):
    """Simula o faturamento: devolve a linha configurada e registra a chamada."""
    estado = {"linha": None, "chamadas": []}

    def fake(nota_fiscal, serie, client):
        estado["chamadas"].append((nota_fiscal, serie))
        return estado["linha"], 1_000_000

    monkeypatch.setattr(bq_client, "_run_validacao_query", fake)
    monkeypatch.setattr(bq_client, "_get_client", lambda: object())
    monkeypatch.setattr(bq_client, "insert_usage_event", lambda **k: None)
    return estado


def _linha(serie, emitida):
    return {"SERIE_NF": serie, "emitida": datetime.fromisoformat(emitida)}


def test_incidente_serie_4_nao_casa_com_serie_72(consulta):
    """O caso real: e-mail fala de 1122836/4; o faturamento só tem 1122836/72.

    A consulta filtra pela série do e-mail, então não há linha — NF descartada."""
    consulta["linha"] = None
    r = bq_client.validar_nf_atacado("1122836", serie="4", hoje=HOJE)
    assert not r.atacado
    assert "série 4" in r.motivo
    assert consulta["chamadas"] == [("1122836", "4")], "a série tem que chegar à consulta"


def test_serie_do_email_confere(consulta):
    consulta["linha"] = _linha("72", "2026-10-03")
    r = bq_client.validar_nf_atacado("1746036", serie="72", hoje=HOJE)
    assert r.atacado
    assert r.serie == "72"
    assert r.idade_dias == 4


def test_com_serie_nf_antiga_nao_e_descartada(consulta):
    """Par (NF, série) exato não é colisão: a trava de idade não se aplica."""
    consulta["linha"] = _linha("72", "2025-08-28")
    r = bq_client.validar_nf_atacado("1122836", serie="72", hoje=HOJE)
    assert r.atacado
    assert r.idade_dias > 180


def test_sem_serie_nf_recente_e_aceita(consulta):
    consulta["linha"] = _linha("72", "2026-09-30")
    r = bq_client.validar_nf_atacado("1746036", hoje=HOJE)
    assert r.atacado
    assert r.serie == "72", "a série casada vai para a WiseReturn"


def test_sem_serie_nf_de_um_ano_e_colisao(consulta):
    """Sem série no e-mail, casar só com NF de ~400 dias é colisão de numeração."""
    consulta["linha"] = _linha("72", "2025-08-28")
    r = bq_client.validar_nf_atacado("1122836", hoje=HOJE)
    assert not r.atacado
    assert "colisão" in r.motivo
    assert r.idade_dias == 405


@pytest.mark.parametrize("dias, aceita", [(180, True), (181, False)])
def test_limite_da_trava_de_idade(consulta, dias, aceita):
    emitida = date.fromordinal(HOJE.toordinal() - dias)
    consulta["linha"] = _linha("72", emitida.isoformat())
    assert bq_client.validar_nf_atacado("1700000", hoje=HOJE).atacado is aceita


def test_nf_inexistente(consulta):
    consulta["linha"] = None
    r = bq_client.validar_nf_atacado("0000000", hoje=HOJE)
    assert not r.atacado
    assert r.serie is None
