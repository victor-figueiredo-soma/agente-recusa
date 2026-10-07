"""Fluxo de processamento de um e-mail, com todas as integrações simuladas.

Confere o efeito ponta a ponta da validação por (NF, série): o que chega ao
Sheets, ao BigQuery e à WiseReturn. Nenhuma chamada real.
"""

import os

for _var in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
             "MAILBOX_USER_ID", "WEBHOOK_CLIENT_STATE", "NOTIFICATION_EMAIL"):
    os.environ.setdefault(_var, "x")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

import main  # noqa: E402
from models.schemas import AnalysisResult, ValidacaoNF, WiseReturnResult  # noqa: E402

_MSG = {
    "id": "msg-1", "conversationId": "conv-1",
    "subject": "Comunicado de Pendência 6718640 (ext)",
    "body": {"content": "..."},
    "from": {"emailAddress": {"address": "sau.ext@braspress.com", "name": "Braspress"}},
    "toRecipients": [], "ccRecipients": [], "receivedDateTime": "2026-09-28T13:43:00Z",
}

# Faturamento simulado: o que existe de verdade no Atacado.
_FATURAMENTO = {
    ("1746036", "72"): ValidacaoNF(atacado=True, serie="72", emitida="2026-10-03", idade_dias=4),
    ("1746036", None): ValidacaoNF(atacado=True, serie="72", emitida="2026-10-03", idade_dias=4),
    # Mesmo número, série 72 de 2025: só casa quando a série NÃO é informada.
    ("1122836", None): ValidacaoNF(
        atacado=False, serie="72", emitida="2025-08-28", idade_dias=405,
        motivo="só casa com NF série 72 emitida há 405 dias — provável colisão"),
}


def _validar(nf, serie=None, thread_id=None):
    return _FATURAMENTO.get(
        (nf, serie), ValidacaoNF(atacado=False, motivo=f"série {serie} não pertence ao Atacado"))


@pytest.fixture
def rodar(monkeypatch):
    monkeypatch.setenv("WISERETURN_API_KEY", "chave-de-teste")

    def _rodar(nota_fiscal, validar=_validar):
        analise = AnalysisResult(
            is_recusa=True, transportadora="braspress", motivo_recusa="Aguardando agendamento",
            sub_motivo="PENDENTE", nota_fiscal=nota_fiscal, confianca="alta",
            tipo_mensagem="padrao_automatico", status="RECUSA",
        )
        with patch.object(main.graph_client, "get_message", return_value=_MSG), \
             patch.object(main.graph_client, "get_conversation_messages", return_value=[]), \
             patch.object(main.graph_client, "send_reply") as reply, \
             patch.object(main, "analyze_email", return_value=analise), \
             patch.object(main.bq_client, "thread_already_processed", return_value=False), \
             patch.object(main.bq_client, "validar_nf_atacado", side_effect=validar), \
             patch.object(main, "write_to_sheet", return_value="primeira") as sheet, \
             patch.object(main.bq_client, "insert_chamado_if_absent") as bq, \
             patch.object(main.wisereturn_client, "criar_bd",
                          return_value=WiseReturnResult(criado=True, numero_bd="1")) as bd, \
             patch.object(main.bq_client, "insert_usage_event"):
            main._process_message("msg-1")
        return sheet, bq, bd, reply

    return _rodar


def test_incidente_serie_4_nao_gera_chamado_nem_bd(rodar):
    """O comunicado real de 28/09: três NFs série 4, que não são do Atacado."""
    consultadas = []

    def validar(nf, serie=None, thread_id=None):
        consultadas.append((nf, serie))
        return _validar(nf, serie, thread_id)

    sheet, bq, bd, reply = rodar("1119516/4, 1119519/4, 1119520/4", validar=validar)
    assert consultadas == [("1119516", "4"), ("1119519", "4"), ("1119520", "4")], \
        "a série do e-mail tem que chegar à validação"
    assert sheet.call_count == 0, "nenhum chamado no Sheets"
    assert bq.call_count == 0, "nenhum chamado no BigQuery"
    assert bd.call_count == 0, "nenhum BD na WiseReturn"
    assert reply.call_count == 0, "nada a avisar à logística"


def test_sem_serie_colisao_antiga_nao_gera_chamado(rodar):
    sheet, bq, bd, _ = rodar("1122836")
    assert sheet.call_count == 0 and bq.call_count == 0 and bd.call_count == 0


def test_nf_legitima_usa_a_serie_do_email(rodar):
    sheet, bq, bd, reply = rodar("1746036/72")
    assert sheet.call_count == 1
    assert bq.call_count == 1
    assert bd.call_args.kwargs["serie"] == "72"
    assert bd.call_args.kwargs["nota_fiscal"] == "1746036"
    assert reply.call_count == 1


def test_sheets_e_bigquery_gravam_so_o_numero(rodar):
    """O formato das colunas não muda: a série não vaza para o registro."""
    sheet, bq, _, _ = rodar("1746036/72")
    assert sheet.call_args.args[0].nota_fiscal == "1746036"
    assert bq.call_args.kwargs["nota_fiscal"] == "1746036"


def test_lote_misto_so_a_legitima_passa(rodar):
    sheet, bq, bd, _ = rodar("1746036/72, 1122836/4")
    assert sheet.call_count == 1
    assert bd.call_count == 1
    assert bd.call_args.kwargs["nota_fiscal"] == "1746036"


def test_validacao_fora_do_ar_registra_mas_nao_abre_bd(rodar):
    """Fail-open do fluxo antigo preservado: com o BigQuery fora, o chamado é
    registrado como sempre foi. Mas o BD exige validação concluída — sem ela não
    há série confiável, e abrir BD para a NF errada é o que causou o incidente."""
    def fora_do_ar(*a, **k):
        raise RuntimeError("BigQuery indisponível")

    sheet, bq, bd, reply = rodar("1746036/72", validar=fora_do_ar)
    assert sheet.call_count == 1, "fail-open: o chamado continua sendo registrado"
    assert bq.call_count == 1
    assert bd.call_count == 0, "sem validação, nenhum BD"
    corpo = reply.call_args.args[1]
    assert "validação da NF indisponível" in corpo, "a logística é avisada do motivo"
