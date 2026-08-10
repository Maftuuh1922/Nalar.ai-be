from datetime import datetime
from pydantic import BaseModel
import uuid
from typing import Any, List, Optional
import json

class SkillResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    content: str
    source: str
    tags: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

class SkillCreate(BaseModel):
    name: str
    description: Optional[str] = None
    content: str
    source: str = "local"
    tags: Optional[str | List[str]] = None
    
    model_config = {"populate_by_name": True}
    
    def model_post_init(self, __context: Any) -> None:
        # Convert tags array to comma-separated string for DB
        if isinstance(self.tags, list):
            self.tags = ",".join(self.tags)

class SkillUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    is_active: Optional[bool] = None
    tags: Optional[str | List[str]] = None
    
    model_config = {"populate_by_name": True}
    
    def model_post_init(self, __context: Any) -> None:
        if isinstance(self.tags, list):
            self.tags = ",".join(self.tags)
