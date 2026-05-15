from pydantic import BaseModel, Field
from typing import Optional


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


class ProcessedEmail(BaseModel):
    message_id: str
    conversation_id: str
    data_hora_recebimento: str
    remetente: str
    transportadora: Optional[str] = None
    assunto: str
    nota_fiscal: Optional[str] = None
    motivo_recusa: Optional[str] = None
    confianca: Optional[str] = None
    tipo_interacao: str = "primeira"
    acao: str = "Chamado Recebido"
