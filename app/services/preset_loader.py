import os
import uuid
from typing import List, Dict, Any
from pydantic import BaseModel

PRESETS_DIR = r"C:\Users\Administrator\Pictures\DeepTutor\deeptutor\services\persona\presets"

class PersonaPreset(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    system_prompt: str
    avatar_icon: str
    is_builtin: bool = True

def _parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content.strip()
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content.strip()
    
    fm_text = parts[1]
    result = {}
    current_key = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line.startswith(" ") and current_key:
            result[current_key] += " " + line.strip()
        else:
            if ":" in line:
                key, val = line.split(":", 1)
                current_key = key.strip()
                result[current_key] = val.strip()
    return result, parts[2].strip()

def get_builtin_personas() -> List[PersonaPreset]:
    personas = []
    if not os.path.exists(PRESETS_DIR):
        return personas
    
    for entry in os.scandir(PRESETS_DIR):
        if entry.is_dir():
            md_path = os.path.join(entry.path, "PERSONA.md")
            if os.path.exists(md_path):
                try:
                    with open(md_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    
                    frontmatter, body = _parse_frontmatter(content)
                    
                    name = frontmatter.get("name", entry.name)
                    desc = frontmatter.get("description", "")
                    
                    personas.append(PersonaPreset(
                        id=uuid.uuid5(uuid.NAMESPACE_OID, name),
                        name=name,
                        role=desc,
                        system_prompt=body,
                        avatar_icon="Bot",
                        is_builtin=True
                    ))
                except Exception as e:
                    import logging
                    logging.error(f"Failed to load preset {entry.name}: {e}")
                    
    return personas
