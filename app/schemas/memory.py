from datetime import datetime
from pydantic import BaseModel
import uuid

class MemoryResponse(BaseModel):
    id: uuid.UUID
    layer: str
    surface: str
    content: str
    metadata_json: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

class MemoryCreate(BaseModel):
    layer: str = "L1"
    surface: str = "chat"
    content: str
    metadata_json: str | None = None

class MemoryStats(BaseModel):
    l1_count: int
    l2_count: int
    l3_count: int
    surfaces: list[str]
