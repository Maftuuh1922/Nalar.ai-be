from datetime import datetime
from pydantic import BaseModel
import uuid

class KnowledgeBaseResponse(BaseModel):
    id: uuid.UUID
    name: str
    engine: str
    description: str | None
    document_count: int
    is_default: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

class KnowledgeBaseCreate(BaseModel):
    name: str
    engine: str = "llamaindex"
    description: str | None = None

class KnowledgeBaseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    engine: str | None = None
    is_default: bool | None = None

class KnowledgeBaseDocumentResponse(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
