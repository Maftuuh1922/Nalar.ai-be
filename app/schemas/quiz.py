from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class QuestionSchema(BaseModel):
    question: str = Field(..., description="The multiple choice question")
    options: list[str] = Field(..., description="List of options, e.g., ['A', 'B', 'C', 'D']")
    answer: str = Field(..., description="The correct option exact string, e.g., 'A' or the full text")
    explanation: str = Field(..., description="Explanation of the correct answer")


class QuizCreate(BaseModel):
    document_id: UUID
    topic: str = Field(..., min_length=1, max_length=255)
    num_questions: int = Field(default=5, ge=1, le=20)


class QuizResponse(BaseModel):
    id: UUID
    user_id: UUID
    document_id: UUID
    topic: str
    questions_data: list[QuestionSchema]
    created_at: datetime

    model_config = {"from_attributes": True}
