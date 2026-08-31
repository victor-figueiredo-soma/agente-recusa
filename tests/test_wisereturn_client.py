"""Testes do cliente WiseReturn.

Mockam `wisereturn_client._session.post` — nenhuma chamada real ao ERP.
Rodar da raiz `agente-recusa/`: pytest -q
"""

import json

import pytest
import requests

from agents import wisereturn_client as wr


class _FakeResp:
    """Dublê de requests.Response com o mínimo que o cliente usa."""

    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("resposta não é JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


@pytest.fixture(autouse=True)
def _chave_configurada(monkeypatch):
    monkeypatch.setenv("WISERETURN_API_KEY", "chave-de-teste")


@pytest.fixture(autouse=True)
def _sem_espera_de_retry(monkeypatch):
    """Neutraliza o backoff do tenacity para os testes de retry não levarem 12s."""
    monkeypatch.setattr(wr._post.retry, "sleep", lambda _: None)


def _mock_post(monkeypatch, **kwargs):
    from unittest.mock import Mock

    post = Mock(**kwargs)
    monkeypatch.setattr(wr._session, "post", post)
    return post


# --- classificação da resposta ------------------------------------------------

def test_bd_criado_isok_maiusculo(monkeypatch):
    """A API real devolve `isOK` (K maiúsculo), não o `isOk` da documentação.

    Ler só `isOk` faria toda resposta de sucesso cair no bucket de erro."""
    _mock_post(monkeypatch, return_value=_FakeResp(200, [
        {"message": "Bd:1042, referente a Nf:123456, criado com sucesso.", "isOK": True,
         "elapsed": [], "totalItems": 0},
        {"message": "Produto:REF001, Cor:01, Tamanho:M, Quantidade:5, inserido no Bd:1042",
         "isOK": True},
    ]))
    r = wr.criar_bd("123456", "motivo qualquer")
    assert r.criado and r.ok
    assert r.numero_bd == "1042"
    assert not r.ja_existia
    assert not r.erro_negocio


def test_bd_criado_isok_documentado(monkeypatch):
    """Se a API for corrigida para o `isOk` da doc, continua funcionando."""
    _mock_post(monkeypatch, return_value=_FakeResp(200, [
        {"message": "Bd:1042, referente a Nf:123456, criado com sucesso.", "isOk": True},
    ]))
    assert wr.criar_bd("123456", "motivo").criado


def test_bd_ja_criado_nao_e_falha(monkeypatch):
    """Idempotência: "Bd já criado" vem com isOK=false, mas o BD EXISTE."""
    _mock_post(monkeypatch, return_value=_FakeResp(200, [
        {"message": "Bd já criado para NF:123456, numero do Bd:1042", "isOK": False},
    ]))
    r = wr.criar_bd("123456", "motivo qualquer")
    assert r.ja_existia and r.ok
    assert r.numero_bd == "1042"
    assert not r.criado
    assert not r.erro_negocio


def test_nf_nao_localizada(monkeypatch):
    _mock_post(monkeypatch, return_value=_FakeResp(200, [
        {"message": "NF:123456 não localizada no ERP.", "isOK": False},
    ]))
    r = wr.criar_bd("123456", "motivo qualquer")
    assert not r.ok
    assert r.erro_negocio
    assert "não localizada no ERP" in r.mensagens[0]


def test_mensagem_obrigatoria(monkeypatch):
    _mock_post(monkeypatch, return_value=_FakeResp(200, [
        {"message": "O campo Mensagem é obrigatório.", "isOK": False},
    ]))
    r = wr.criar_bd("123456", "")
    assert r.erro_negocio and not r.ok


def test_cliente_nao_localizado(monkeypatch):
    _mock_post(monkeypatch, return_value=_FakeResp(200, [
        {"message": "Cliente cnpj:12345678000199 não localizado.", "isOK": False},
    ]))
    assert wr.criar_bd("123456", "motivo").erro_negocio


# --- erros de transporte ------------------------------------------------------

def test_401_nao_faz_retry(monkeypatch):
    post = _mock_post(monkeypatch, return_value=_FakeResp(401, [], text="Unauthorized"))
    r = wr.criar_bd("123456", "motivo")
    assert r.erro_auth and not r.ok
    assert post.call_count == 1, "401 é config errada — retry só triplica a latência"


def test_401_string_crua(monkeypatch):
    """A API real devolve uma string JSON no 401, não um objeto."""
    post = _mock_post(monkeypatch, return_value=_FakeResp(401, "API Key inválida."))
    r = wr.criar_bd("123456", "motivo")
    assert r.erro_auth
    assert post.call_count == 1


def test_500_sem_envelope_faz_retry_e_vira_erro_rede(monkeypatch):
    """Trava a regressão do raise_for_status() CONDICIONAL em _post.

    Sem ele, o 5xx voltaria como Response normal e não haveria retry algum —
    o predicado de utils/retry.py só enxerga o status via exceção HTTPError.
    Corpo irreconhecível (HTML do App Service) = falha transitória de verdade."""
    post = _mock_post(monkeypatch, return_value=_FakeResp(500, None, text="<html>502</html>"))
    r = wr.criar_bd("123456", "motivo")
    assert r.erro_rede and not r.ok
    assert post.call_count == 3


def test_500_com_envelope_nao_faz_retry(monkeypatch):
    """Comportamento REAL da NF não localizada: HTTP 500 + envelope de erro.

    É determinístico (verificado em 3 tentativas idênticas contra a API), então
    tem que ser classificado como erro de negócio e NÃO retentado."""
    post = _mock_post(monkeypatch, return_value=_FakeResp(500, {
        "id": "A1",
        "messages": [{"id": "99", "text": "Ocorreram erros ao processar essa solicitacao.",
                      "data": "", "alertType": 3}],
        "success": False,
        "url": "https://wisereturnapi-soma.azurewebsites.net/service.asmx/external/importacaoRecusa/bds",
    }))
    r = wr.criar_bd("0000000", "motivo")
    assert r.erro_negocio and not r.ok
    assert not r.erro_rede
    assert post.call_count == 1, "500 determinístico não deve ser retentado"
    # O status entra no resumo: o envelope da API não diz qual foi.
    assert "HTTP 500" in r.resumo
    assert "Ocorreram erros" in r.resumo


def test_timeout_vira_erro_rede(monkeypatch):
    post = _mock_post(monkeypatch, side_effect=requests.exceptions.Timeout("read timeout"))
    r = wr.criar_bd("123456", "motivo")
    assert r.erro_rede and not r.ok
    assert post.call_count == 3


def test_resposta_nao_json(monkeypatch):
    """O endpoint é /service.asmx/... e pode devolver HTML em erro de infra."""
    _mock_post(monkeypatch, return_value=_FakeResp(200, None, text="<html>502 Bad Gateway</html>"))
    r = wr.criar_bd("123456", "motivo")
    assert r.erro_rede and not r.ok


def test_criar_bd_nunca_levanta(monkeypatch):
    _mock_post(monkeypatch, side_effect=RuntimeError("boom"))
    r = wr.criar_bd("123456", "motivo")  # não deve propagar
    assert not r.ok and not r.erro_rede


# --- feature flag -------------------------------------------------------------

def test_sem_chave_e_noop(monkeypatch):
    monkeypatch.delenv("WISERETURN_API_KEY", raising=False)
    post = _mock_post(monkeypatch, return_value=_FakeResp(200, []))
    r = wr.criar_bd("123456", "motivo")
    assert r.desabilitado
    assert post.call_count == 0, "sem chave não pode nem tocar na rede"
    assert not wr.habilitado()


def test_nf_vazia_nao_chama_api(monkeypatch):
    post = _mock_post(monkeypatch, return_value=_FakeResp(200, []))
    r = wr.criar_bd("", "motivo")
    assert not r.ok
    assert post.call_count == 0


# --- montar_message -----------------------------------------------------------

def test_message_nunca_vazio():
    m = wr.montar_message("1528101", "RECUSA")
    assert m and "1528101" in m
    assert "\n" not in m


def test_message_usa_motivo_recusa():
    m = wr.montar_message("1528101", "RECUSA", "Pedido cancelado", "PEDIDO_CANCELADO", "braspress")
    assert "Pedido cancelado" in m
    assert "[PEDIDO_CANCELADO]" in m
    assert "BRASPRESS" in m


def test_message_fallback_submotivo():
    m = wr.montar_message("1528101", "RECUSA", None, "PEDIDO_CANCELADO")
    assert "Pedido cancelado" in m


def test_message_verbo_por_status():
    assert "retida por questão fiscal" in wr.montar_message("1", "RETENÇÃO FISCAL")
    assert "extraviada" in wr.montar_message("1", "EXTRAVIO")
    assert "recusada" in wr.montar_message("1", "RECUSA")
    assert "recusada" in wr.montar_message("1", "STATUS_DESCONHECIDO")


def test_message_truncado_e_uma_linha():
    m = wr.montar_message("1528101", "RECUSA", "a\nb " * 500)
    assert len(m) <= wr._MAX_MESSAGE_LEN
    assert "\n" not in m
