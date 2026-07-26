from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class QuizAttemptCreate(BaseModel):
    # `quiz_id` diambil dari path URL; di body sifatnya opsional agar klien
    # cukup mengirim skornya saja.
    quiz_id: UUID | None = None
    score_percentage: int = Field(..., ge=0, le=100)


class QuizAttemptResponse(BaseModel):
    id: UUID
    quiz_id: UUID
    user_id: UUID
    score_percentage: int
    created_at: datetime

    model_config = {"from_attributes": True}
