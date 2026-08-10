"""Ekstraksi metadata jurnal dari PDF via LLM (background task).

Pola mengikuti ``deep_research.run_research``: dipanggil dari BackgroundTasks,
membuka session DB sendiri, dan memperbarui baris ``journal_references``.

Alur:
1. Baca teks halaman 1-2 PDF (pymupdf/fitz — sudah dipakai di proyek).
2. Kirim ke LLM user (resolve via model_configs) minta JSON metadata.
3. Simpan hasil ke kolom metadata; status -> extracted | failed.

Metadata diekstrak otomatis karena user upload file; hasilnya WAJIB bisa
diedit manual di UI (AI tidak selalu benar).
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.journal import JournalReference

logger = logging.getLogger(__name__)

_SYSTEM_EXTRACTOR = (
    "Kamu adalah pustakawan akademik yang teliti. Dari teks halaman awal sebuah "
    "jurnal ilmiah, ekstrak metadata bibliografinya. Balas HANYA JSON valid tanpa "
    "markdown fence, dengan skema: "
    '{"title": string, "authors": [string], "year": int|null, '
    '"journal_name": string, "volume": string|null, "issue": string|null, '
    '"pages": string|null, "doi": string|null, "publisher": string|null, '
    '"abstract": string|null}. '
    "Gunakan null bila informasi tidak ditemukan di teks. Jangan mengarang. "
    "Nama penulis dalam format 'NamaBelakang, NamaDepan'."
)

# Halaman pertama + kedua biasanya memuat judul, penulis, dan afiliasi.
_PDF_PAGE_CHARS = 4000
_MAX_PAGES = 2


def _extract_pdf_text(file_path: str) -> str:
    """Baca teks dari PDF via pymupdf; fallback kosong bila gagal."""
    try:
        import fitz  # pymupdf

        doc = fitz.open(file_path)
        parts: list[str] = []
        for page in doc.pages(0, min(_MAX_PAGES, doc.page_count)):
            text = page.get_text().strip()
            if text:
                parts.append(text[:_PDF_PAGE_CHARS])
        doc.close()
        return "\n\n".join(parts)
    except Exception as exc:  # noqa: BLE001 — ekstraksi teks tidak boleh mematikan alur
        logger.warning("Gagal membaca PDF %s: %s", file_path, exc)
        return ""


async def _extract_web_text(url: str) -> str:
    """Baca teks dari URL web (referensi dari link/chat)."""
    try:
        from app.services.document_tools import fetch_webpage

        import json as _json

        raw = _json.loads(await fetch_webpage(url, max_chars=_PDF_PAGE_CHARS))
        if raw.get("error"):
            logger.warning("Gagal membaca URL %s: %s", url, raw["error"])
            return ""
        return (raw.get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gagal membaca URL %s: %s", url, exc)
        return ""


def _parse_llm_json(raw: str) -> dict:
    """Ambil objek JSON dari balasan model; kembalikan dict kosong bila gagal."""
    text = (raw or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.+?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                return {}
        else:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def run_extract_metadata(
    *,
    reference_id: str,
    base_url: str,
    api_key: str,
    model_name: str,
    db_url: str,
) -> None:
    """Ekstrak metadata satu baris ``journal_references`` (dipanggil background)."""
    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    async with session_factory() as db:
        reference = await db.get(JournalReference, uuid.UUID(reference_id))
        if reference is None:
            await engine.dispose()
            return

        try:
            reference.status = "extracting"
            await db.commit()

            # Sumber bisa file PDF lokal atau URL (referensi dari link/chat).
            file_path = reference.file_path or ""
            if file_path.startswith(("http://", "https://")):
                pdf_text = await _extract_web_text(file_path)
            else:
                pdf_text = _extract_pdf_text(file_path)
            if not pdf_text:
                reference.status = "failed"
                reference.error_message = "Tidak dapat membaca teks dari sumber (PDF/URL)."
                await db.commit()
                await engine.dispose()
                return

            client = AsyncOpenAI(base_url=base_url, api_key=api_key or "dummy", timeout=180.0)
            response = await client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": _SYSTEM_EXTRACTOR},
                    {
                        "role": "user",
                        "content": (
                            f"Ekstrak metadata dari teks jurnal berikut "
                            f"(file: {reference.filename}):\n\n{pdf_text[:8000]}"
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=1500,
            )
            raw = (response.choices[0].message.content or "").strip()
            meta = _parse_llm_json(raw)

            if not meta:
                reference.status = "failed"
                reference.error_message = "AI tidak dapat mengekstrak metadata (respons tidak valid)."
                await db.commit()
                await engine.dispose()
                return

            reference.title = _clean_str(meta.get("title")) or reference.filename
            authors = meta.get("authors")
            reference.authors = (
                [str(a).strip() for a in authors if str(a).strip()]
                if isinstance(authors, list)
                else None
            )
            year = meta.get("year")
            reference.year = int(year) if str(year).strip().isdigit() else None
            reference.journal_name = _clean_str(meta.get("journal_name")) or ""
            reference.volume = _clean_str(meta.get("volume"))
            reference.issue = _clean_str(meta.get("issue"))
            reference.pages = _clean_str(meta.get("pages"))
            reference.doi = _clean_str(meta.get("doi"))
            reference.publisher = _clean_str(meta.get("publisher"))
            abstract = _clean_str(meta.get("abstract"))
            reference.abstract = abstract
            reference.status = "extracted"
            reference.error_message = None
            await db.commit()

        except Exception as exc:  # noqa: BLE001
            logger.exception("Ekstraksi metadata %s gagal", reference_id)
            reference.status = "failed"
            reference.error_message = str(exc)[:900]
            await db.commit()

    await engine.dispose()
