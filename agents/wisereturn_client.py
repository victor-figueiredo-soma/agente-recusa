"""Cliente da API WiseReturn — "Importação de BD por Recusa".

Cria o Boletim de Devolução (BD) a partir do número da NF recusada. O BD nasce
com status PENDENTE ANALISTA; CNPJ, representante, transportadora e itens são
buscados automaticamente no ERP pela própria API — não precisamos informá-los.

PARTICULARIDADE DA API: erros de NEGÓCIO voltam no CORPO, não no status. Por isso
este módulo NÃO usa raise_for_status() geral — ver _post().

DIVERGÊNCIA DA DOCUMENTAÇÃO, verificada contra a API real: o campo de sucesso é
`isOK` (K maiúsculo), não `isOk` como documentado na seção 6.1. Ler só `isOk`
faria toda resposta de sucesso parecer falha — ver _item_ok().

O campo `serie` é obrigatório desde a versão da doc que o introduziu; omiti-lo
devolve "O campo Serie é obrigatório.". A série vem do BigQuery
(bq_client.buscar_serie_nf), pois o e-mail da transportadora não a informa.
"""

import os
import re

import requests

from models.schemas import WiseReturnResult
from utils.logger import get_logger
from utils.retry import transient_retry

logger = get_logger(__name__)

_DEFAULT_URL = (
    "https://wisereturnapi-soma.azurewebsites.net"
    "/service.asmx/external/importacaoRecusa/bds"
)

# Sessão única reaproveitada (keep-alive + pool), no mesmo molde do graph_client.
_session = requests.Session()

# (connect, read). Maior que os 15s do graph_client de propósito: o host é um
# Azure App Service e o primeiro request após ociosidade paga cold start.
_TIMEOUT = (10, 30)

# Trava defensiva: o tamanho da coluna de histórico no ERP é desconhecido, e um
# texto longo poderia virar um 500 do servidor.
_MAX_MESSAGE_LEN = 500

# "Bd já criado para NF:123456, numero do Bd:1042"
# Precisa ser testado ANTES do padrão de sucesso: essa mensagem de ERRO também
# contém o literal "Bd:1042", que casaria com _RE_BD_QUALQUER.
_RE_BD_JA_CRIADO = re.compile(
    r"bd\s+j[áa]\s+criado.*?numero\s+do\s+bd:\s*(\d+)", re.IGNORECASE | re.DOTALL
)
# "Bd:1042, referente a Nf:123456, criado com sucesso."
_RE_BD_CRIADO = re.compile(
    r"bd:\s*(\d+).{0,80}?criado com sucesso", re.IGNORECASE | re.DOTALL
)
# "Produto:REF001, ..., inserido no Bd:1042" — fallback para o número do BD.
_RE_BD_QUALQUER = re.compile(r"\bbd:\s*(\d+)", re.IGNORECASE)

# O status canônico do AnalysisResult vira o verbo do histórico do BD.
_VERBO_POR_STATUS = {
    "RECUSA": "recusada",
    "RETENÇÃO FISCAL": "retida por questão fiscal",
    "EXTRAVIO": "extraviada",
}


def habilitado() -> bool:
    """A integração roda apenas com WISERETURN_API_KEY definida.

    Sem a chave é um no-op silencioso: uma integração nova não pode quebrar o
    registro do chamado nem impedir `import main` em teste local."""
    return bool(os.environ.get("WISERETURN_API_KEY", "").strip())


def montar_message(
    nota_fiscal: str,
    status: str,
    motivo_recusa: str | None = None,
    sub_motivo: str | None = None,
    transportadora: str | None = None,
) -> str:
    """Texto do campo `message` — obrigatório na API e VISÍVEL ao analista no
    histórico do BD. Precisa ser autoexplicativo e nunca vazio (a API rejeita
    com "O campo Mensagem é obrigatório.").

    Cadeia de fallback do motivo: motivo_recusa (texto livre do Gemini, o mais
    legível) → sub_motivo padronizado humanizado → literal genérico."""
    verbo = _VERBO_POR_STATUS.get((status or "").strip().upper(), "recusada")

    motivo = (motivo_recusa or "").strip()
    if not motivo and sub_motivo:
        motivo = sub_motivo.replace("_", " ").capitalize()
    if not motivo:
        motivo = "motivo não informado pela transportadora"
    if sub_motivo:
        motivo = f"{motivo} [{sub_motivo}]"

    transp = (transportadora or "não identificada").strip().upper()
    texto = (
        f"NF {nota_fiscal} {verbo}. Motivo: {motivo}. Transportadora: {transp}. "
        f"Registro automático do Agente de Recusa (Atacado)."
    )
    # Colapsa em uma única linha: motivo_recusa vem de LLM e pode conter quebras
    # que bagunçam o histórico do BD.
    return re.sub(r"\s+", " ", texto).strip()[:_MAX_MESSAGE_LEN]


def _item_ok(item: dict) -> bool:
    """True se o item marca sucesso.

    A doc diz `isOk`; a API real devolve `isOK`. Comparamos a chave em minúsculas
    para aceitar as duas e não depender de qual lado corrigir primeiro."""
    return any(k.lower() == "isok" and v is True for k, v in item.items())


def _extrair(resp: requests.Response) -> tuple[list[str], bool] | None:
    """Normaliza os DOIS formatos de resposta observados, em (mensagens, algum_ok).

    A) documentado, lista:  [{"message": "...", "isOK": true}, ...]
    B) envelope de erro:    {"success": false, "messages": [{"text": "..."}]}

    Devolve None quando o corpo não é um payload reconhecível da WiseReturn (HTML
    ou XML de erro de infra, string crua) — é isso que distingue um erro
    determinado da aplicação de uma falha de transporte."""
    try:
        payload = resp.json()
    except ValueError:
        return None

    if isinstance(payload, list):
        itens = [i for i in payload if isinstance(i, dict)]
        if not itens:
            return None
        return [str(i.get("message", "")) for i in itens], any(_item_ok(i) for i in itens)

    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        msgs = [str(m.get("text", "")) for m in payload["messages"] if isinstance(m, dict)]
        return msgs, payload.get("success") is True

    return None


@transient_retry
def _post(nf: str, serie: str, message: str, api_key: str) -> requests.Response:
    """POST cru. Levanta HTTPError — acionando o retry — SOMENTE em 5xx cujo corpo
    NÃO é um payload da WiseReturn.

    Duas armadilhas motivam essa condição exata:

    1. O transient_retry (utils/retry.py) decide pela EXCEÇÃO. Sem levantar nada,
       um 5xx voltaria como Response normal e não haveria retry algum. Já um
       raise_for_status() geral faria retry de 401 (config errada — inútil e 3x
       mais lento).
    2. Um 5xx que vem COM o envelope da aplicação é erro determinado — retentar
       só gasta latência. Já um 5xx SEM envelope reconhecível (HTML do App
       Service em cold start ou restart) é transitório e merece as 3 tentativas.
       Hoje os erros de negócio chegam como 200, mas a discriminação fica: uma
       versão anterior da API respondia 500 para NF não localizada."""
    url = os.environ.get("WISERETURN_API_URL", _DEFAULT_URL)
    resp = _session.post(
        url,
        json={"nf": nf, "serie": serie, "message": message},
        headers={"X-External-Key": api_key, "Content-Type": "application/json"},
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 500 and _extrair(resp) is None:
        resp.raise_for_status()
    return resp


def _classificar(mensagens: list[str], algum_ok: bool) -> WiseReturnResult:
    """Traduz a resposta normalizada no desfecho de negócio."""
    texto = " | ".join(mensagens)

    # "BD já criado" é a resposta de idempotência da API — não é falha real.
    m = _RE_BD_JA_CRIADO.search(texto)
    if m:
        return WiseReturnResult(ja_existia=True, numero_bd=m.group(1), mensagens=mensagens)

    if algum_ok:
        m = _RE_BD_CRIADO.search(texto) or _RE_BD_QUALQUER.search(texto)
        return WiseReturnResult(
            criado=True, numero_bd=m.group(1) if m else None, mensagens=mensagens
        )

    return WiseReturnResult(mensagens=mensagens)  # erro de negócio


def criar_bd(nota_fiscal: str, serie: str, message: str) -> WiseReturnResult:
    """Cria o BD da NF na WiseReturn.

    NUNCA levanta exceção — sempre devolve um WiseReturnResult e faz o logging do
    desfecho. O chamador (main.py) só decide o que mostrar no e-mail de resumo."""
    if not habilitado():
        return WiseReturnResult(desabilitado=True)
    if not nota_fiscal:
        return WiseReturnResult(mensagens=["NF vazia — chamada não realizada"])
    if not serie:
        # A API responderia "O campo Serie é obrigatório."; poupa o round-trip.
        return WiseReturnResult(mensagens=["série vazia — chamada não realizada"])

    api_key = os.environ["WISERETURN_API_KEY"].strip()
    try:
        resp = _post(nota_fiscal, serie, message, api_key)
    except requests.RequestException as e:
        # Timeout/conexão/5xx após os 3 retries. WARNING (não ERROR): o chamado
        # interno já está gravado e o BD pode ser recriado por reenvio idempotente.
        logger.warning(
            f"WiseReturn indisponível — BD da NF {nota_fiscal}/{serie} não criado: {e}"
        )
        return WiseReturnResult(erro_rede=True, mensagens=[str(e)])
    except Exception:
        # Bug nosso. ERROR (dispara e-mail de alerta) com texto FIXO sem a NF, para
        # o throttle de 300s do EmailAlertHandler colapsar um lote inteiro num
        # único e-mail; o detalhe por NF vai no warning abaixo e o traceback no
        # exc_info (que não entra na chave do throttle).
        logger.error("WiseReturn: exceção inesperada ao criar BD", exc_info=True)
        logger.warning(f"WiseReturn: exceção inesperada na NF {nota_fiscal}")
        return WiseReturnResult(mensagens=["exceção inesperada"])

    if resp.status_code == 401:
        # ERROR: afeta TODAS as NFs (nenhum BD é criado) — precisa de alerta.
        # Texto fixo, sem a NF, pelo mesmo motivo de throttle acima.
        logger.error(
            "WiseReturn 401 — WISERETURN_API_KEY ausente ou inválida; "
            "nenhum BD está sendo criado"
        )
        return WiseReturnResult(erro_auth=True, mensagens=["401 não autorizado"])

    extraido = _extrair(resp)
    if extraido is None:
        # Corpo irreconhecível: o endpoint é /service.asmx/... e devolve HTML/XML
        # em erro de infra. Só chega aqui em status < 500 (5xx sem envelope já
        # virou exceção de rede no _post).
        logger.warning(
            f"WiseReturn: resposta não reconhecida (HTTP {resp.status_code}) "
            f"para NF {nota_fiscal}: {resp.text[:300]}"
        )
        return WiseReturnResult(erro_rede=True, mensagens=[resp.text[:300]])

    mensagens, algum_ok = extraido
    if resp.status_code >= 400:
        # O envelope de erro da API não diz o status; anexá-lo dá diagnosticabilidade
        # ao log e ao e-mail de resumo (um 500 aqui é tipicamente NF fora do ERP).
        mensagens = [f"HTTP {resp.status_code}"] + mensagens
    result = _classificar(mensagens, algum_ok)

    if result.criado:
        logger.info(f"WiseReturn — NF {nota_fiscal} série {serie}: BD {result.numero_bd} criado")
    elif result.ja_existia:
        logger.info(
            f"WiseReturn — NF {nota_fiscal}: BD {result.numero_bd} já existia (no-op)"
        )
    else:
        # WARNING (não ERROR): erro de negócio é dado POR NF — como ERROR, um lote
        # de 20 NFs viraria 20 e-mails (o throttle não colapsa, a NF está no texto).
        # Esses casos aparecem agregados no e-mail de resumo à logística.
        logger.warning(
            f"WiseReturn recusou a criação do BD da NF {nota_fiscal}/{serie}: {result.resumo}"
        )
    return result
