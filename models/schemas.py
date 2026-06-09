import re
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List


# Sub-motivos padronizados (fonte única da verdade — usada na validação do
# AnalysisResult e injetada no prompt do Gemini). Vão para a coluna MOTIVO da
# tabela de chamados. PENDENTE é o fallback quando não há enquadramento claro.
SUBMOTIVOS: tuple[str, ...] = (
    "ACORDO_COMERCIAL",
    "AGUARDANDO_PGTO_ICMS_ANTECIPADO",
    "ESTABELECIMENTO_FECHADO",
    "EXTRAVIO_PARCIAL",
    "EXTRAVIO_TOTAL",
    "FALTA_PECA_CAIXA_COM_AVARIA",
    "FALTA_PECA_CAIXA_LACRADA",
    "FATURADO_PARA_CNPJ_INCORRETO",
    "FORA_DO_PRAZO",
    "GRADE_QUEBRADA",
    "MUDOU_DE_ENDERECO",
    "PEDIDO_CANCELADO",
    "PROMOCAO_DO_SITE",
    "SOBRA_DE_VOLUME",
    "TRANSPORTADORA_NAO_ATENDE_REGIAO",
    "TROCA_DE_VOLUME",
    "VALOR_DE_RECEBIMENTO_ALTO",
    "ENTREGA_REALIZADA",
    "PENDENTE",
    "DIVERGENCIA_INTERNA",
)

SUBMOTIVO_FALLBACK = "PENDENTE"


class EmailPayload(BaseModel):
    subject: str
    body: str
    receivedDateTime: str
    from_email: str = Field(alias="from")
    fromName: Optional[str] = None
    messageId: str
    conversationId: str

    model_config = {"populate_by_name": True}


class AnalysisResult(BaseModel):
    is_recusa: bool
    transportadora: Optional[str] = None
    motivo_recusa: Optional[str] = None  # descrição livre (vai para o Sheets)
    sub_motivo: Optional[str] = None  # categoria padronizada (vai para a coluna MOTIVO do BQ)
    nota_fiscal: Optional[str] = None
    confianca: Optional[str] = None
    tipo_mensagem: Optional[str] = None
    status: Optional[str] = None  # "RECUSA" ou "RETENÇÃO FISCAL"

    @field_validator("nota_fiscal")
    @classmethod
    def validate_nota_fiscal(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        valid = [nf.strip() for nf in v.split(",") if re.fullmatch(r"\d{7}", nf.strip())]
        return ", ".join(valid) if valid else None

    @field_validator("sub_motivo")
    @classmethod
    def validate_sub_motivo(cls, v: Optional[str]) -> Optional[str]:
        """Garante que o sub_motivo é exatamente uma das categorias padronizadas.
        Qualquer valor fora da lista (ou ausente) cai no fallback PENDENTE — assim
        a coluna MOTIVO do BQ nunca recebe um valor não-controlado, o que manteria
        a chave de dedup (NF, STATUS, MOTIVO) estável."""
        if v is None:
            return None
        normalized = v.strip().upper()
        return normalized if normalized in SUBMOTIVOS else SUBMOTIVO_FALLBACK

    @model_validator(mode="after")
    def enforce_submotivo_rules(self):
        """Invariantes de negócio do sub_motivo, garantidas em código (não dependem
        do acerto do LLM):
        - Retenção fiscal SEMPRE mapeia para AGUARDANDO_PGTO_ICMS_ANTECIPADO.
        - Recusa sem enquadramento cai no fallback PENDENTE, para a coluna MOTIVO
          do BQ nunca ficar nula (mantém a chave de dedup estável)."""
        status_norm = (self.status or "").strip().upper()
        if status_norm in ("RETENÇÃO FISCAL", "RETENCAO FISCAL"):
            self.sub_motivo = "AGUARDANDO_PGTO_ICMS_ANTECIPADO"
        elif self.is_recusa and not self.sub_motivo:
            self.sub_motivo = SUBMOTIVO_FALLBACK
        return self


class ProcessedEmail(BaseModel):
    message_id: str
    conversation_id: str
    data_hora_recebimento: str
    remetente: str
    transportadora: Optional[str] = None
    tipo_mensagem: Optional[str] = None
    assunto: str
    nota_fiscal: Optional[str] = None
    motivo_recusa: Optional[str] = None
    confianca: Optional[str] = None
    status: str = "RECUSA"
    tipo_interacao: str = "primeira"
    acao: str = "Chamado Recebido"


# --- Microsoft Graph Change Notification schemas ---

class GraphResourceData(BaseModel):
    id: Optional[str] = Field(default=None, alias="id")
    odata_type: Optional[str] = Field(default=None, alias="@odata.type")

    model_config = {"populate_by_name": True}


class GraphNotificationItem(BaseModel):
    subscriptionId: Optional[str] = None
    changeType: Optional[str] = None
    resource: Optional[str] = None
    resourceData: Optional[GraphResourceData] = None
    clientState: Optional[str] = None


class GraphNotificationPayload(BaseModel):
    value: List[GraphNotificationItem]
