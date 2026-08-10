"""Endpoint Dashboard (PRD v2.3 §3.1).

Menampilkan:
- Progres per bab (Kosong/Draft/Review/Final) untuk dokumen Co-Writer aktif.
- Ringkasan referensi: jumlah jurnal per grup laporan.
- Shortcut "Lanjutkan menulis" → dokumen Co-Writer terakhir diubah.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.models.co_writer import CoWriterDocument
from app.models.journal import JournalGroup, JournalReference
from app.models.user import User

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _section_status(section_heading: str) -> str:
    """Status bab dari teks heading: Final/Review/Draft/Kosong via tanda."""
    # Heuristic: heading dengan isi > 300 char = Draft; ada TODO = Draft;
    # > 800 char & tanpa TODO = Review; diakhiri "FINAL" = Final.
    # Dipakai untuk ringkasan cepat; status akurat ditentukan user di editor.
    text = section_heading.lower()
    if "final" in text:
        return "Final"
    if "review" in text:
        return "Review"
    if "draft" in text or "todo" in text:
        return "Draft"
    return "Kosong"


@router.get("")
async def dashboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Data dashboard: dokumen aktif + progres bab + ringkasan referensi."""
    # Dokumen Co-Writer terbaru (untuk shortcut lanjut menulis)
    docs = (
        await db.scalars(
            select(CoWriterDocument)
            .where(CoWriterDocument.user_id == current_user.id)
            .order_by(CoWriterDocument.updated_at.desc())
            .limit(10)
        )
    ).all()

    # Progres bab dari dokumen pertama (aktif)
    active_doc = docs[0] if docs else None
    sections: list[dict] = []
    if active_doc:
        lines = (active_doc.content or "").splitlines()
        current_heading = None
        current_body = []
        for line in lines:
            if re.match(r"^#{1,3}\s", line.strip()):
                if current_heading is not None:
                    sections.append(
                        {
                            "heading": current_heading,
                            "status": _section_status(" ".join(current_body)),
                            "chars": sum(len(b) for b in current_body),
                        }
                    )
                current_heading = line.strip()
                current_body = []
            else:
                current_body.append(line)
        if current_heading is not None:
            sections.append(
                {
                    "heading": current_heading,
                    "status": _section_status(" ".join(current_body)),
                    "chars": sum(len(b) for b in current_body),
                }
            )

    # Ringkasan referensi per grup
    groups = (
        await db.scalars(
            select(JournalGroup).where(JournalGroup.user_id == current_user.id)
        )
    ).all()
    group_summaries = []
    for g in groups:
        count = await db.scalar(
            select(func.count())
            .select_from(JournalReference)
            .where(
                JournalReference.group_id == g.id,
                JournalReference.user_id == current_user.id,
            )
        )
        group_summaries.append({"id": str(g.id), "name": g.name, "count": count or 0})

    return {
        "active_doc": (
            {
                "id": str(active_doc.id),
                "title": active_doc.title or "Untitled draft",
                "updated_at": int(active_doc.updated_at.timestamp()),
            }
            if active_doc
            else None
        ),
        "sections": sections,
        "group_summaries": group_summaries,
        "total_references": sum(g["count"] for g in group_summaries),
    }
