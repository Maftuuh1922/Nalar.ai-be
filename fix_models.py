import os
import re

models_dir = r"c:\Users\Administrator\Documents\project ta\Nalar.ai-be\app\models"

for file in os.listdir(models_dir):
    if not file.endswith(".py"): continue
    filepath = os.path.join(models_dir, file)
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Replacements
    content = content.replace("from sqlalchemy.dialects.postgresql import UUID\n", "")
    content = content.replace("from sqlalchemy.dialects.postgresql import JSONB, UUID\n", "")
    content = content.replace("from sqlalchemy.dialects.postgresql import JSONB\n", "")
    
    content = content.replace("UUID(as_uuid=True)", "Uuid(as_uuid=True)")
    content = content.replace("JSONB", "JSON")
    
    # Add Uuid and JSON to sqlalchemy imports
    if "Uuid" not in content and "Uuid(" in content:
        content = content.replace("from sqlalchemy import ", "from sqlalchemy import Uuid, ")
    if "JSON" not in content and "mapped_column(JSON" in content:
        content = content.replace("from sqlalchemy import ", "from sqlalchemy import JSON, ")

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
