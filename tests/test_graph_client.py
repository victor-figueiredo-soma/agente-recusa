"""Testes do cleanup de subscriptions do Graph.

O foco é o filtro por notificationUrl: `GET /subscriptions` devolve as
subscriptions de todo o App Registration, então sem esse filtro uma instância
deleta a subscription de outra. Nenhuma chamada real ao Graph.
"""

import pytest

from agents import graph_client as gc

_NOSSA_URL = "https://agente-recusa-bd.up.railway.app/graph-webhook"
_OUTRA_URL = "https://agente-recusa-antigo.up.railway.app/graph-webhook"

_CAIXA_ATUAL = "logistica.atacado@somagrupo.com.br"
_RESOURCE_ATUAL = f"users/{_CAIXA_ATUAL}/mailFolders/Inbox/messages"
_RESOURCE_OUTRA_CAIXA = "users/gabriela.wajzenberg@somagrupo.com.br/mailFolders/Inbox/messages"


@pytest.fixture(autouse=True)
def _caixa_configurada(monkeypatch):
    monkeypatch.setenv("MAILBOX_USER_ID", _CAIXA_ATUAL)


@pytest.fixture
def deletadas(monkeypatch):
    """Coleta os ids que o cleanup tentaria deletar."""
    ids = []
    monkeypatch.setattr(gc, "_delete_subscription", lambda sub_id: ids.append(sub_id))
    return ids


def _com_subs(monkeypatch, subs):
    monkeypatch.setattr(gc, "_list_subscriptions", lambda: subs)


def test_preserva_subscription_de_outra_instancia(monkeypatch, deletadas):
    """O caso que motivou a correção: outra instância, outra caixa.

    Antes do filtro por URL, esta subscription era vista como "obsoleta" (caixa
    diferente da nossa) e deletada a cada startup e a cada ciclo do watchdog."""
    _com_subs(monkeypatch, [
        {"id": "outra-inst", "resource": _RESOURCE_OUTRA_CAIXA, "notificationUrl": _OUTRA_URL},
    ])
    gc.cleanup_stale_subscriptions(_NOSSA_URL)
    assert deletadas == [], "não pode tocar em subscription de outra instância"


def test_deleta_a_nossa_de_caixa_antiga(monkeypatch, deletadas):
    """Obsolescência real: MAILBOX_USER_ID mudou, a subscription velha é nossa."""
    _com_subs(monkeypatch, [
        {"id": "nossa-velha", "resource": _RESOURCE_OUTRA_CAIXA, "notificationUrl": _NOSSA_URL},
    ])
    gc.cleanup_stale_subscriptions(_NOSSA_URL)
    assert deletadas == ["nossa-velha"]


def test_preserva_a_nossa_da_caixa_correta(monkeypatch, deletadas):
    _com_subs(monkeypatch, [
        {"id": "nossa-ok", "resource": _RESOURCE_ATUAL, "notificationUrl": _NOSSA_URL},
    ])
    gc.cleanup_stale_subscriptions(_NOSSA_URL)
    assert deletadas == []


def test_cenario_misto(monkeypatch, deletadas):
    """Só a nossa de caixa antiga sai; as outras três ficam."""
    _com_subs(monkeypatch, [
        {"id": "nossa-velha", "resource": _RESOURCE_OUTRA_CAIXA, "notificationUrl": _NOSSA_URL},
        {"id": "nossa-ok", "resource": _RESOURCE_ATUAL, "notificationUrl": _NOSSA_URL},
        {"id": "outra-inst", "resource": _RESOURCE_OUTRA_CAIXA, "notificationUrl": _OUTRA_URL},
        {"id": "outra-mesma-caixa", "resource": _RESOURCE_ATUAL, "notificationUrl": _OUTRA_URL},
    ])
    gc.cleanup_stale_subscriptions(_NOSSA_URL)
    assert deletadas == ["nossa-velha"]


def test_url_normalizada(monkeypatch, deletadas):
    """Barra final e caixa não devem fazer a subscription parecer de outro."""
    _com_subs(monkeypatch, [
        {"id": "nossa-com-barra", "resource": _RESOURCE_OUTRA_CAIXA,
         "notificationUrl": _NOSSA_URL.upper() + "/"},
    ])
    gc.cleanup_stale_subscriptions(_NOSSA_URL)
    assert deletadas == ["nossa-com-barra"]


def test_sem_notification_url_e_preservada(monkeypatch, deletadas):
    """Campo ausente: não dá para provar que é nossa, então não se apaga."""
    _com_subs(monkeypatch, [
        {"id": "sem-url", "resource": _RESOURCE_OUTRA_CAIXA},
    ])
    gc.cleanup_stale_subscriptions(_NOSSA_URL)
    assert deletadas == []


def test_falha_ao_listar_nao_propaga(monkeypatch, deletadas):
    def explode():
        raise RuntimeError("Graph fora do ar")

    monkeypatch.setattr(gc, "_list_subscriptions", explode)
    gc.cleanup_stale_subscriptions(_NOSSA_URL)  # não deve levantar
    assert deletadas == []


def test_falha_ao_deletar_nao_interrompe(monkeypatch):
    """Uma falha de delete não pode abortar a limpeza das demais."""
    tentadas = []

    def delete_falhando(sub_id):
        tentadas.append(sub_id)
        if sub_id == "primeira":
            raise RuntimeError("403")

    monkeypatch.setattr(gc, "_delete_subscription", delete_falhando)
    _com_subs(monkeypatch, [
        {"id": "primeira", "resource": _RESOURCE_OUTRA_CAIXA, "notificationUrl": _NOSSA_URL},
        {"id": "segunda", "resource": _RESOURCE_OUTRA_CAIXA, "notificationUrl": _NOSSA_URL},
    ])
    gc.cleanup_stale_subscriptions(_NOSSA_URL)
    assert tentadas == ["primeira", "segunda"]
