from datetime import datetime
from pydantic import BaseModel
import uuid

class QuestionResponse(BaseModel):
    id: uuid.UUID
    topic: str
    question: str
    user_answer: str | None
    reference_answer: str | None
    explanation: str | None
    tags: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

class QuestionCreate(BaseModel):
    topic: str
    question: str
    user_answer: str | None = None
    reference_answer: str | None = None
    explanation: str | None = None
    tags: str | None = None

class QuestionUpdate(BaseModel):
    user_answer: str | None = None
    reference_answer: str | None = None
    explanation: str | None = None
    tags: str | None = None
