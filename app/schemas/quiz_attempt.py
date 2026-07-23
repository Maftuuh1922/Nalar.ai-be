from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class QuizAttemptCreate(BaseModel):
    quiz_id: UUID
    score_percentage: int


class QuizAttemptResponse(BaseModel):
    id: UUID
    quiz_id: UUID
    user_id: UUID
    score_percentage: int
    created_at: datetime

    model_config = {"from_attributes": True}
