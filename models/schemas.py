import re
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List


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
    motivo_recusa: Optional[str] = None
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
