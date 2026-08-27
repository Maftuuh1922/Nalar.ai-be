"""Endpoint Co-Writer: CRUD dokumen + AI edit / automark / edit seleksi streaming.

Kontrak mengikuti ``lib/co-writer-api.ts`` dan halaman ``co-writer/[docId]``:
- timestamp dikirim sebagai epoch detik (int)
- list mengembalikan ``{documents: [...]}`` dengan preview
- edit â†’ ``{edited_text}``, automark â†’ ``{marked_text}``
- edit_react/stream â†’ SSE ``event: stream|content|result|error``
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.config import settings
from app.models.co_writer import CoWriterDocument
from app.models.co_writer_file import CoWriterFile, JalurTidakSah, bersihkan_jalur
from app.models.co_writer_folder import CoWriterFolder
from app.models.user import User
from app.schemas.co_writer import (
    AgenticWriteRequest,
    AgenticWriteResponse,
    CoWriterAutoMarkRequest,
    CoWriterAutoMarkResponse,
    CoWriterCreate,
    CoWriterDeletedOut,
    CoWriterDocumentOut,
    CoWriterEditRequest,
    CoWriterEditResponse,
    CoWriterFolderCreate,
    CoWriterFolderListOut,
    CoWriterFolderResponse,
    CoWriterFolderUpdate,
    CoWriterListOut,
    CoWriterMoveRequest,
    CoWriterStreamEditRequest,
    CoWriterSummaryOut,
    CoWriterUpdate,
    ImportChatRequest,
    LearningSpaceData,
)
from app.services.agentic_writer import agentic_write
from app.services.agent_run import run_agent_stream
from app.services.citation_tools import referensi_urut
from app.services.academic_reference_search import search_academic_references
from app.services.latex_export import (
    compile_latex_pdf,
    datarkan_input,
    escape_latex_text,
    judul_dari_latex,
    latex_to_markdown,
    localize_latex_image_paths,
    markdown_to_latex,
    pastikan_latex,
)
from app.services.co_writer_llm import (
    CO_WRITER_REQUEST_TIMEOUT_SECONDS,
    CoWriterLLM,
    build_automark_prompt,
    build_full_edit_prompt,
    build_selection_prompt,
    gather_rag_context,
    gather_web_context,
)
from app.services.model_selection import (
    ModelSelectionError,
    ResolvedLLM,
    resolve_embedding,
    resolve_llm,
)
from app.services.research_chat import (
    DocumentEvidence,
    RESEARCH_CHAT_SYSTEM,
    ReferenceEvidence,
    build_research_prompt,
    classify_research_mode,
    format_history,
    input_char_budget,
    output_token_budget,
    select_document_evidence,
    select_reference_evidence,
    should_use_web,
    validate_citations,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/co_writer", tags=["co_writer"])

_SOURCE_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".tex": "application/x-tex",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}


def _ai_timeout_detail() -> str:
    return (
        f"Model aktif tidak merespons dalam {CO_WRITER_REQUEST_TIMEOUT_SECONDS} detik. "
        "Coba lagi atau pilih model yang lebih cepat di pengaturan model."
    )


# --------------------------------------------------------------------------- #
# Utilitas
# --------------------------------------------------------------------------- #


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def _document_upload_dir(doc_id: uuid.UUID) -> Path:
    return settings.upload_dir_abs / str(doc_id)


def _source_file(doc_id: uuid.UUID) -> tuple[Path, str] | None:
    source_dir = _document_upload_dir(doc_id) / "source"
    for extension, media_type in _SOURCE_MEDIA_TYPES.items():
        candidate = source_dir / f"original{extension}"
        if candidate.is_file():
            return candidate, media_type
    return None


def _store_source_file(doc_id: uuid.UUID, extension: str, contents: bytes) -> Path:
    source_dir = _document_upload_dir(doc_id) / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / f"original{extension}"
    target.write_bytes(contents)
    return target


def _onlyoffice_token(doc_id: uuid.UUID) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        str(doc_id).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _verify_onlyoffice_token(doc_id: uuid.UUID, token: str) -> None:
    if not hmac.compare_digest(token or "", _onlyoffice_token(doc_id)):
        raise HTTPException(status_code=403, detail="Token ONLYOFFICE tidak sah.")


def _onlyoffice_docx_path(doc_id: uuid.UUID) -> Path:
    return _document_upload_dir(doc_id) / "onlyoffice" / "document.docx"


def _onlyoffice_pdf_path(doc_id: uuid.UUID) -> Path:
    return _document_upload_dir(doc_id) / "onlyoffice" / "document.pdf"


def _pipeline_sidecar_path(doc_id: uuid.UUID) -> Path:
    return _document_upload_dir(doc_id) / "onlyoffice" / "pipeline.json"


def _baca_sidecar_pipeline(doc_id: uuid.UUID) -> dict:
    """Baca sidecar pipeline; {} bila belum ada / rusak.

    Sidecar mencatat versi pipeline impor yang membangun DOCX kerja dan apakah
    pengguna sudah pernah menyuntingnya (`user_edited`). Dipakai auto-heal di
    `_prepare_onlyoffice_docx` untuk memutuskan bangun-ulang tanpa menghapus
    hasil kerja pengguna.
    """
    path = _pipeline_sidecar_path(doc_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _tulis_sidecar_pipeline(doc_id: uuid.UUID, *, user_edited: bool) -> None:
    """Tulis sidecar pipeline dengan versi impor sekarang."""
    from app.services.docx_postprocess import _PIPELINE_IMPOR_VERSI

    path = _pipeline_sidecar_path(doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pipeline": _PIPELINE_IMPOR_VERSI, "user_edited": bool(user_edited)}),
        encoding="utf-8",
    )


def _derive_title(content: str) -> str:
    """Judul draf diturunkan dari heading pertama (LaTeX atau Markdown lama)."""
    baris = (content or "").splitlines()
    # Heading LaTeX diprioritaskan dan dicari di SELURUH isi, bukan hanya baris
    # pertama: preamble (\documentclass, \usepackage) selalu mendahului
    # \section, jadi berhenti di baris non-kosong pertama akan mengambil
    # "\documentclass[12pt,a4paper]{article}" sebagai judul.
    for line in baris:
        judul = judul_dari_latex(line)
        if judul:
            return judul[:255]
    # Draf lama yang belum dikonversi: heading Markdown.
    for line in baris:
        if line.lstrip().startswith("\\"):
            continue  # perintah LaTeX, bukan teks judul
        cleaned = re.sub(r"^#+\s*", "", line).strip()
        if cleaned:
            return cleaned[:255]
    return "Untitled draft"


def _preview(content: str) -> str:
    """Cuplikan isi draf sebagai teks biasa.

    Draf Co-Writer berisi LaTeX, jadi membuang simbol Markdown saja tidak cukup:
    preamble mendahului naskah, sehingga cuplikannya jadi
    "\\documentclass[12pt,a4paper]{article} \\usepackage..." pada tiap kartu.
    Perintah struktural dibuang seluruhnya, sedangkan perintah biasa
    dipertahankan argumen tekstualnya (``\\section{Hasil}`` → "Hasil").
    """
    text = content or ""
    # BOM / zero-width dari berkas impor: tak terlihat tapi ikut terhitung ke
    # batas 160 karakter dan merusak encoding di sebagian terminal.
    text = text.replace("﻿", "").replace("​", "")
    # Sebagian draf hasil impor lama ter-escape ganda: `\clearpage` tersimpan
    # sebagai `\textbackslash{}clearpage`. Dipulihkan jadi backslash biasa lebih
    # dulu supaya perintahnya dikenali dan dibuang di langkah berikutnya.
    text = re.sub(r"\\textbackslash\s*(?:\\?\{\\?\})?", lambda _: "\\", text)
    # Komentar LaTeX (% sampai akhir baris) bukan naskah. Template kampus
    # membuka dengan blok komentar soal margin, jadi tanpa langkah ini itulah
    # yang muncul di kartu. `\%` yang di-escape adalah persen literal — dijaga.
    text = re.sub(r"(?<!\\)%[^\n]*", " ", text)
    # Sebagian draf hasil impor lama kehilangan kurung kurawalnya, sehingga
    # tersimpan sebagai `\vspace0.3cm` alih-alih `\vspace{0.3cm}`. Ukurannya
    # ikut dibuang di sini; kalau tidak, cuplikannya diawali "0.3cm".
    text = re.sub(
        r"\\(?:vspace|hspace|vskip|hskip)\*?\s*-?[\d.]+\s*(?:cm|mm|in|pt|em|ex|bp|pc)\b",
        " ",
        text,
    )
    # Preamble & perintah yang argumennya bukan naskah — buang beserta argumennya.
    text = re.sub(
        r"\\(?:documentclass|usepackage|geometry|setlength|newcommand|renewcommand"
        r"|bibliographystyle|bibliography|graphicspath|includegraphics"
        r"|label|ref|cite\w*|input|include"
        r"|hypersetup|definecolor|pagestyle|title|author|date"
        # Perintah tata letak: argumennya ukuran (\vspace{0.3cm}), bukan teks.
        r"|vspace|hspace|vskip|hskip|rule|addcontentsline|columnwidth|textwidth)"
        r"\*?\s*(?:\[[^\]]*\])?\s*(?:\{[^{}]*\})*",
        " ",
        text,
    )
    text = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}(?:\[[^\]]*\])?", " ", text)
    # Sisanya: pertahankan isi kurung kurawal — \textbf{penting} → "penting".
    text = re.sub(r"\\[a-zA-Z]+\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)  # perintah tanpa argumen
    text = re.sub(r"[#>*`_~\[\](){}\\]+", " ", text)  # sisa markup Markdown/LaTeX
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160] or "Empty draft"


def _sumber_tex(doc: CoWriterDocument) -> str:
    """Kode LaTeX siap kompilasi dari sebuah draf.

    Draf normal sudah berisi kode LaTeX; yang belum dikonversi (mis. diambil
    lewat rute yang tidak melewati get_document) dilewatkan markdown_to_latex
    supaya ekspor tetap bekerja alih-alih menghasilkan PDF berisi teks mentah.
    """
    isi = doc.content or ""
    if doc.content_format == "latex":
        return isi
    return markdown_to_latex(isi)


async def _muat_berkas(db: AsyncSession, doc_id) -> dict[str, str]:
    """Semua berkas anak sebuah proyek sebagai {jalur: isi}."""
    rows = await db.execute(
        select(CoWriterFile).where(CoWriterFile.doc_id == doc_id)
    )
    return {f.path: f.content or "" for f in rows.scalars().all()}

async def _sumber_tex_proyek(db: AsyncSession, doc: CoWriterDocument) -> str:
    """Sumber LaTeX yang `\\input{}`-nya sudah didatarkan.

    Dipakai setiap kali sebuah rute perlu melihat *naskah utuh*: kompilasi,
    ekspor, outline, gap-analysis, konteks chat. Kalau salah satunya tetap
    membaca `doc.content` mentah, yang dilihatnya cuma preamble + daftar
    `\\input` â€” outline melaporkan nol heading dan PDF-nya jadi beberapa halaman.

    Proyek berkas tunggal (mayoritas draf) tidak punya baris di `co_writer_files`,
    jadi jalurnya sama persis seperti sebelumnya, hanya menambah satu kueri.
    """
    berkas = await _muat_berkas(db, doc.id)
    sumber = _sumber_tex(doc)
    return datarkan_input(sumber, berkas) if berkas else sumber


_OUTLINE_HEADING_RE = re.compile(
    r"(?m)^[ \t]*\\(section|subsection|subsubsection)\*?\s*\{([^{}\r\n]*)\}"
)


def _ringkas_outline_teks(teks: str, limit: int = 240) -> tuple[str, int]:
    """Ambil ringkasan ringan dari isi bagian tanpa memanggil model AI."""
    bersih = re.sub(r"(?m)%.*$", "", teks)
    bersih = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^]]*\])?", " ", bersih)
    bersih = re.sub(r"[{}$]", " ", bersih)
    bersih = re.sub(r"\s+", " ", bersih).strip()
    kata = len(re.findall(r"\b\w+\b", bersih, flags=re.UNICODE))
    if len(bersih) > limit:
        potong = bersih[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
        bersih = potong + "..."
    return bersih, kata


def _ekstrak_outline(berkas: dict[str, str]) -> list[dict]:
    """Ekstrak heading LaTeX/Markdown dengan lokasi berkas dan offset editor."""
    hasil: list[dict] = []
    for path, content in berkas.items():
        source = content or ""
        matches = list(_OUTLINE_HEADING_RE.finditer(source))
        if not matches:
            # Draf lama Markdown tetap dapat dinavigasi selama migrasi format.
            matches = list(re.finditer(r"(?m)^[ \t]*(#{1,3})\s+(.+?)\s*$", source))
            parsed = [(m, len(m.group(1)), m.group(2).strip()) for m in matches]
        else:
            parsed = [
                (
                    m,
                    {"section": 1, "subsection": 2, "subsubsection": 3}[m.group(1)],
                    m.group(2).strip(),
                )
                for m in matches
            ]
        for index, (match, level, title) in enumerate(parsed):
            start = match.end()
            end = parsed[index + 1][0].start() if index + 1 < len(parsed) else len(source)
            summary, word_count = _ringkas_outline_teks(source[start:end])
            hasil.append(
                {
                    "path": path,
                    "level": level,
                    "title": title,
                    "offset": match.start(),
                    "summary": summary,
                    "word_count": word_count,
                }
            )
    return hasil


def _sumber_preview_proyek(
    sumber_utama: str,
    berkas: dict[str, str],
    *,
    isi_editor: str | None = None,
    jalur_editor: str | None = None,
    isi_diberikan: bool = False,
) -> str:
    """Bangun sumber proyek dengan buffer editor menggantikan berkas aktif."""
    utama = sumber_utama
    berkas_preview = dict(berkas)

    if isi_diberikan:
        if isi_editor is None:
            raise ValueError("content harus berupa teks")
        if jalur_editor:
            jalur = bersihkan_jalur(jalur_editor)
            if jalur == "main.tex":
                utama = isi_editor
            else:
                berkas_preview[jalur] = isi_editor
        else:
            utama = isi_editor

    return datarkan_input(utama, berkas_preview) if berkas_preview else utama


_BIBLIOGRAPHY_START = "% NALAR-AI:BIBLIOGRAPHY:START"
_BIBLIOGRAPHY_END = "% NALAR-AI:BIBLIOGRAPHY:END"
_BIBLIOGRAPHY_BLOCK_RE = re.compile(
    rf"(?ms)^[ \t]*{re.escape(_BIBLIOGRAPHY_START)}.*?^[ \t]*{re.escape(_BIBLIOGRAPHY_END)}[ \t]*(?:\r?\n)?"
)
_LEGACY_LATEX_BIBLIOGRAPHY_RE = re.compile(
    r"(?ms)^[ \t]*\\section\*?\s*\{Daftar Pustaka\}.*?(?=^[ \t]*\\end\s*\{document\})"
)


def _perbarui_bibliografi_latex(content: str, entries: list[str]) -> tuple[str, str]:
    """Replace the generated bibliography block without damaging LaTeX source."""
    items = (
        "\n".join(f"\\item {escape_latex_text(entry)}" for entry in entries)
        if entries
        else "\\item Belum ada sitasi di dokumen."
    )
    section = (
        f"{_BIBLIOGRAPHY_START}\n"
        "\\section*{Daftar Pustaka}\n"
        "\\begin{enumerate}\n"
        f"{items}\n"
        "\\end{enumerate}\n"
        f"{_BIBLIOGRAPHY_END}"
    )
    cleaned = _BIBLIOGRAPHY_BLOCK_RE.sub("", content or "")
    cleaned = _LEGACY_LATEX_BIBLIOGRAPHY_RE.sub("", cleaned).rstrip()
    end_match = list(re.finditer(r"\\end\s*\{document\}", cleaned))
    if end_match:
        pos = end_match[-1].start()
        updated = f"{cleaned[:pos].rstrip()}\n\n{section}\n\n{cleaned[pos:]}"
    else:
        updated = f"{cleaned}\n\n{section}\n"
    return updated, section


_CHAPTER_SPLIT_RE = re.compile(r"(?m)^[ \t]*\\chapter\*?\s*\{[^}\r\n]*\}")
# \section MAUPUN \section*: impor DOCX sengaja memakai bentuk berbintang karena
# nomor bab sudah tertulis di teks judulnya ("1.1 Latar Belakang"), jadi menuntut
# bentuk tanpa bintang membuat "Pecah per Bab" mustahil dipakai pada dokumen yang
# justru paling butuh dipecah — laporan hasil impor.
_SECTION_SPLIT_RE = re.compile(r"(?m)^[ \t]*\\section\*?\s*\{([^}\r\n]*)\}")
# Konvensi judul bab khas laporan Indonesia: "Bab 1", "BAB I PENDAHULUAN", dst.
_BAB_TITLE_RE = re.compile(r"^bab\s+(\d+|[ivxlcdm]+)", re.IGNORECASE)
_END_DOCUMENT_RE = re.compile(r"\\end\s*\{document\}")


def _judul_tanpa_format(judul: str) -> str:
    """Judul section tanpa perintah format (\\textbf{...}, \\large, dst)."""
    teks = re.sub(r"\\[a-zA-Z]+\*?", " ", judul or "")
    teks = teks.replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", teks).strip()


def _slug_judul(judul: str) -> str:
    """Slug ASCII stabil untuk nama berkas bab."""
    teks = unicodedata.normalize("NFKD", judul)
    teks = teks.encode("ascii", "ignore").decode("ascii").lower()
    teks = re.sub(r"\\[a-zA-Z]+", " ", teks)
    teks = re.sub(r"[^a-z0-9]+", "-", teks).strip("-")
    return teks[:80] or "bab"


def _pecah_per_bab(sumber: str) -> tuple[str, list[tuple[str, str]]]:
    """Pecah tesis pada chapter atau artikel pada section.

    Titik pecah hanya di `\\chapter` (bintang maupun tidak) atau `\\section`
    tanpa bintang. Bila dokumen memakai konvensi judul bab "Bab N"/"BAB N"
    (umum di laporan TA), hanya section yang judulnya cocok yang dipecah â€”
    kalau tidak, sampul, daftar isi, dan sub-bab ikut menjadi berkas bab.
    """
    cocok = list(_CHAPTER_SPLIT_RE.finditer(sumber or ""))
    if not cocok:
        semua = list(_SECTION_SPLIT_RE.finditer(sumber or ""))
        if not semua:
            raise ValueError(
                "Dokumen tidak memiliki \\chapter atau \\section yang dapat dipecah."
            )
        judul_tiap = [_judul_tanpa_format(m.group(1)) for m in semua]
        bab_pattern = [
            m for m, judul in zip(semua, judul_tiap) if _BAB_TITLE_RE.match(judul)
        ]
        cocok = bab_pattern if bab_pattern else semua

    awalan = sumber[: cocok[0].start()].rstrip()
    potongan: list[str] = []
    for indeks, section in enumerate(cocok):
        akhir = cocok[indeks + 1].start() if indeks + 1 < len(cocok) else len(sumber)
        potongan.append(sumber[section.start() : akhir])

    akhiran = ""
    end_document = _END_DOCUMENT_RE.search(potongan[-1])
    if end_document:
        akhiran = potongan[-1][end_document.start() :].lstrip()
        potongan[-1] = potongan[-1][: end_document.start()]

    bab: list[tuple[str, str]] = []
    for nomor, isi in enumerate(potongan, start=1):
        isi = isi.strip()
        judul = _judul_tanpa_format(judul_dari_latex(isi.splitlines()[0])) if isi else None
        if not judul:
            raise ValueError(f"Judul section ke-{nomor} tidak dapat dibaca.")
        jalur = f"bab/{nomor:02d}-{_slug_judul(judul)}.tex"
        bab.append((jalur, isi + "\n"))

    bagian_utama = [awalan, "\n".join(f"\\input{{{jalur}}}" for jalur, _ in bab)]
    if akhiran:
        bagian_utama.append(akhiran.rstrip())
    utama = "\n\n".join(bagian for bagian in bagian_utama if bagian).rstrip() + "\n"
    return utama, bab


def _ringkas_berkas(berkas: CoWriterFile) -> dict:
    return {
        "path": berkas.path,
        "size": len((berkas.content or "").encode("utf-8")),
        "updated_at": _epoch(berkas.updated_at),
    }


async def _get_file(db: AsyncSession, doc_id: uuid.UUID, path: str) -> CoWriterFile:
    berkas = await db.scalar(
        select(CoWriterFile).where(
            CoWriterFile.doc_id == doc_id,
            CoWriterFile.path == path,
        )
    )
    if berkas is None:
        raise HTTPException(status_code=404, detail="Berkas tidak ditemukan.")
    return berkas


def _summary(doc: CoWriterDocument) -> CoWriterSummaryOut:
    return CoWriterSummaryOut(
        id=doc.id,
        title=doc.title or "Untitled draft",
        created_at=_epoch(doc.created_at),
        updated_at=_epoch(doc.updated_at),
        preview=_preview(doc.content),
        folder_id=doc.folder_id,
    )


def _detail(doc: CoWriterDocument) -> CoWriterDocumentOut:
    source = _source_file(doc.id)
    return CoWriterDocumentOut(
        id=doc.id,
        title=doc.title,
        content=doc.content,
        created_at=_epoch(doc.created_at),
        updated_at=_epoch(doc.updated_at),
        source_format=source[0].suffix.lstrip(".") if source else None,
        # Tanpa ini frontend tidak punya cara mengetahui isi `content` berupa LaTeX
        # atau Markdown lama, padahal editor CodeMirror memilih mode pewarnaan
        # sintaks berdasarkan itu. Draf lama tetap None sampai dikonversi.
        content_format=doc.content_format,
    )


async def _get_owned_doc(
    db: AsyncSession,
    doc_id: uuid.UUID,
    user: User,
) -> CoWriterDocument:
    doc = await db.scalar(
        select(CoWriterDocument).where(
            CoWriterDocument.id == doc_id,
            CoWriterDocument.user_id == user.id,
        )
    )
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dokumen tidak ditemukan.",
        )
    return doc


async def _resolve_llm(
    db: AsyncSession,
    user_id,
    selection=None,
) -> ResolvedLLM:  # noqa: ANN001 â€” payload pilihan model dinamis
    try:
        return await resolve_llm(db, user_id, selection)
    except ModelSelectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


async def _try_embedding(db: AsyncSession, user_id):
    """Resolusi model embedding; None bila belum dikonfigurasi (tool rag non-fatal)."""
    try:
        return await resolve_embedding(db, user_id)
    except ModelSelectionError:
        return None


async def _noop_trace(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
    """Trace kosong untuk endpoint non-streaming."""


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chat_to_markdown(messages) -> str:
    """Ubah daftar ChatHistory menjadi markdown ringkas (untuk ekspor ke draf)."""
    parts: list[str] = []
    for msg in messages:
        role = "ðŸ‘¤ User" if msg.role == "user" else "ðŸ¤– AI"
        content = (msg.content or "").strip()
        if not content:
            continue
        # Batasi panjang tiap pesan agar draf tidak meledak.
        if len(content) > 3000:
            content = content[:3000] + "\n\n_[dipotong]_"
        parts.append(f"### {role}\n\n{content}")
    if not parts:
        return "_Sesi chat kosong._"
    return "\n\n".join(parts)


async def _docx_file_dari_markdown(
    db: AsyncSession,
    user: User,
    markdown: str,
    title: str,
) -> tuple[str, str]:
    """Buat DOCX template kampus + sitasi DOI dari markdown (jalur ekspor P1).

    Mengembalikan (path, filename). Template resmi kampus dipakai bila ada;
    URL DOI diubah menjadi hyperlink aktif.
    """
    import os
    import re as _re
    import tempfile

    from app.models.journal import JournalReference
    from app.services.docx_template_exporter import (
        markdown_to_docx_template as markdown_to_docx,
    )
    from starlette.concurrency import run_in_threadpool

    # Penomoran [n] memakai satu sumber kebenaran bersama tool `cite_add` agen.
    references: dict[int, str] = {}
    ordered = await referensi_urut(db, user.id)
    for i, ref in enumerate(ordered, start=1):
        doi = (ref.doi or "").strip()
        if doi:
            references[i] = doi if doi.startswith("http") else f"https://doi.org/{doi}"

    # Rapatkan penomoran sitasi [1..N] urut kemunculan untuk keluaran ini, lalu
    # petakan ulang tabel DOI ke nomor baru — keduanya HARUS berkunci nomor yang
    # sama supaya hyperlink [n] menunjuk referensi yang benar. Pemanggil WAJIB
    # mengirim markdown mentah (belum dirapatkan) agar pemetaan ini konsisten.
    from app.services.citation_tools import naskah_ekspor_rapat

    markdown, _peta_sitasi = await naskah_ekspor_rapat(db, user.id, markdown)
    if _peta_sitasi:
        references = {
            _peta_sitasi[lama]: doi
            for lama, doi in references.items()
            if lama in _peta_sitasi
        }

    safe_title = _re.sub(r'[\\/:*?"<>|]', "_", title or "Draf").strip()[:80] or "Draf"
    output_path = os.path.join(
        tempfile.gettempdir(),
        f"{safe_title.replace(' ', '_')}_{uuid.uuid4().hex[:6]}.docx",
    )
    await run_in_threadpool(markdown_to_docx, markdown, output_path, references)
    return output_path, f"{safe_title}.docx"


async def _file_pdf_dari_markdown(
    db: AsyncSession,
    user: User,
    markdown: str,
    title: str,
    *,
    images_dir: str,
    fname: str,
) -> FileResponse:
    """PDF dari markdown: pandoc â†’ tectonic, fallback DOCX â†’ HTML-to-PDF.

    Jalur ekspor baru (P1) yang dipakai mode Sync â€” sumber markdown datang
    dari pipeline SFDT â†’ DOCX â†’ Markdown, bukan dari kolom LaTeX.
    """
    import os
    import tempfile

    from starlette.concurrency import run_in_threadpool

    from app.services import pandoc_latex

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", title or "Draf").strip()[:80] or "Draf"
    tmpdir = tempfile.mkdtemp()
    output_path = os.path.join(tmpdir, f"{fname}.pdf")

    # Jalur PDF (pandoc/typeset) tak memakai tabel DOI — cukup rapatkan teksnya
    # agar sitasi keluar [1..N] urut kemunculan, bukan mulai dari tengah ([13]).
    # Fallback DOCX di bawah TETAP menerima `markdown` mentah karena
    # `_docx_file_dari_markdown` merapatkan + memetakan DOI-nya sendiri.
    from app.services.citation_tools import naskah_ekspor_rapat

    md_rapat, _ = await naskah_ekspor_rapat(db, user.id, markdown)
    try:
        pdf_path = pandoc_latex.pandoc_to_pdf(
            md_rapat,
            output_path,
            asset_dirs=[images_dir] if images_dir else [],
            jobname=fname,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Export PDF (pandoc/tectonic) gagal untuk %s: %s",
            exc, exc_info=True,
        )
        alasan = str(exc).strip().splitlines()[-1][:160] if str(exc).strip() else "kompilasi gagal"
        import unicodedata as _u

        alasan_ascii = _u.normalize("NFKD", alasan).encode("ascii", "ignore").decode()
        try:
            docx_path, docx_filename = await _docx_file_dari_markdown(
                db, user, markdown, title
            )
            return FileResponse(
                path=docx_path,
                filename=docx_filename,
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                headers={
                    "X-Fallback-Notice": (
                        "Export PDF (LaTeX) gagal karena "
                        f"{alasan_ascii}; dibuatkan versi DOCX sebagai gantinya - "
                        "bisa dikonversi ke PDF lewat Word."
                    )
                },
            )
        except Exception as exc2:  # noqa: BLE001
            logger.error(
                "Fallback DOCX gagal untuk %s: %s",
                exc2, exc_info=True,
            )
        try:
            from app.services.typeset import typeset_to_pdf

            await run_in_threadpool(typeset_to_pdf, md_rapat, output_path)
        except Exception as exc3:  # noqa: BLE001
            logger.error(
                "Fallback HTML-to-PDF gagal untuk %s: %s",
                exc3, exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "Export PDF gagal di semua jalur (LaTeX, Word, pratinjau). "
                    "Coba periksa kembali isi dokumen, atau hubungi admin dengan "
                    f"pesan berikut: {alasan}"
                ),
            )
        return FileResponse(
            path=output_path,
            filename=f"{safe_title}.pdf",
            media_type="application/pdf",
            headers={
                "X-Fallback-Notice": (
                    "Export PDF (LaTeX) gagal karena "
                    f"{alasan_ascii}; PDF dibuat dari pratinjau layar sebagai gantinya."
                )
            },
        )
    return FileResponse(
        path=pdf_path,
        filename=f"{safe_title}.pdf",
        media_type="application/pdf",
    )


# --------------------------------------------------------------------------- #
# Folder (pengelompokan draf, boleh bersarang)
# --------------------------------------------------------------------------- #

# Kedalaman maksimum pohon folder. Dua alasan: pohon yang lebih dalam tidak lagi
# terbaca di sidebar selebar 200px, dan setiap penelusuran leluhur/keturunan jadi
# terbatas — tidak ada rekursi tak berujung meski data sempat melingkar.
MAX_FOLDER_DEPTH = 5


async def _folder_map(db: AsyncSession, user_id) -> dict[uuid.UUID, CoWriterFolder]:
    """Semua folder milik user sebagai {id: folder}.

    Pohon folder seorang user berukuran puluhan baris, jadi dimuat sekali lalu
    ditelusuri di memori. Itu menghindari kueri rekursif (SQLite lama tidak punya
    CTE) dan satu kueri per tingkat saat memeriksa leluhur.
    """
    rows = await db.scalars(select(CoWriterFolder).where(CoWriterFolder.user_id == user_id))
    return {f.id: f for f in rows.all()}


def _ancestor_ids(folders: dict[uuid.UUID, CoWriterFolder], folder_id: uuid.UUID) -> list[uuid.UUID]:
    """Rantai id dari `folder_id` sendiri naik sampai folder akar."""
    chain: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    current: uuid.UUID | None = folder_id
    while current is not None and current in folders and current not in seen:
        seen.add(current)
        chain.append(current)
        current = folders[current].parent_id
    return chain


def _descendant_ids(folders: dict[uuid.UUID, CoWriterFolder], folder_id: uuid.UUID) -> set[uuid.UUID]:
    """`folder_id` beserta seluruh keturunannya."""
    hasil = {folder_id}
    lapis = {folder_id}
    while lapis:
        lapis = {f.id for f in folders.values() if f.parent_id in lapis and f.id not in hasil}
        hasil |= lapis
    return hasil


def _subtree_height(folders: dict[uuid.UUID, CoWriterFolder], folder_id: uuid.UUID) -> int:
    """Jumlah tingkat pada subpohon `folder_id` (folder tanpa anak = 1)."""
    anak = [f.id for f in folders.values() if f.parent_id == folder_id]
    if not anak:
        return 1
    return 1 + max(_subtree_height(folders, a) for a in anak)


async def _get_owned_folder(db: AsyncSession, folder_id: uuid.UUID, user: User) -> CoWriterFolder:
    folder = await db.scalar(
        select(CoWriterFolder).where(
            CoWriterFolder.id == folder_id,
            CoWriterFolder.user_id == user.id,
        )
    )
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder tidak ditemukan.")
    return folder


async def _direct_counts(db: AsyncSession, user_id) -> dict[uuid.UUID, int]:
    """Jumlah draf per folder, hanya yang langsung berada di folder itu."""
    rows = await db.execute(
        select(CoWriterDocument.folder_id, func.count())
        .where(
            CoWriterDocument.user_id == user_id,
            CoWriterDocument.folder_id.is_not(None),
        )
        .group_by(CoWriterDocument.folder_id)
    )
    return {fid: jumlah for fid, jumlah in rows.all() if fid is not None}


def _folder_response(
    folder: CoWriterFolder,
    folders: dict[uuid.UUID, CoWriterFolder],
    langsung: dict[uuid.UUID, int],
) -> CoWriterFolderResponse:
    """Satu folder untuk respons, dengan hitungan yang mencakup subfolder.

    Hitungannya inklusif supaya angka di sidebar cocok dengan jumlah kartu yang
    muncul saat folder itu dipilih — memilih folder induk juga menampilkan isi
    subfoldernya.
    """
    return CoWriterFolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        color=folder.color,
        document_count=sum(langsung.get(fid, 0) for fid in _descendant_ids(folders, folder.id)),
        created_at=_epoch(folder.created_at),
    )


@router.get("/folders", response_model=CoWriterFolderListOut)
async def list_folders(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterFolderListOut:
    """Semua folder milik user beserta jumlah draf (termasuk isi subfolder)."""
    folders = await _folder_map(db, current_user.id)
    langsung = await _direct_counts(db, current_user.id)
    urut = sorted(folders.values(), key=lambda f: (f.name.lower(), f.created_at))
    return CoWriterFolderListOut(
        folders=[_folder_response(f, folders, langsung) for f in urut]
    )


@router.post("/folders", response_model=CoWriterFolderResponse, status_code=status.HTTP_201_CREATED)
async def create_folder(
    payload: CoWriterFolderCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterFolderResponse:
    """Buat folder baru, opsional di dalam folder lain."""
    folders = await _folder_map(db, current_user.id)
    if payload.parent_id is not None:
        if payload.parent_id not in folders:
            raise HTTPException(status_code=404, detail="Folder induk tidak ditemukan.")
        if len(_ancestor_ids(folders, payload.parent_id)) >= MAX_FOLDER_DEPTH:
            raise HTTPException(
                status_code=400,
                detail=f"Folder maksimal {MAX_FOLDER_DEPTH} tingkat.",
            )

    folder = CoWriterFolder(
        user_id=current_user.id,
        parent_id=payload.parent_id,
        name=payload.name.strip(),
        color=(payload.color or "").strip() or None,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    folders[folder.id] = folder
    return _folder_response(folder, folders, {})


@router.put("/folders/{folder_id}", response_model=CoWriterFolderResponse)
async def update_folder(
    folder_id: uuid.UUID,
    payload: CoWriterFolderUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterFolderResponse:
    """Ganti nama/warna folder, atau pindahkan ke induk lain."""
    folder = await _get_owned_folder(db, folder_id, current_user)
    folders = await _folder_map(db, current_user.id)

    if payload.name is not None and payload.name.strip():
        folder.name = payload.name.strip()
    if payload.color is not None:
        folder.color = payload.color.strip() or None

    # "parent_id": null berarti pindah ke akar, jadi dibedakan dari field yang
    # tidak dikirim sama sekali.
    if "parent_id" in payload.model_fields_set:
        induk_baru = payload.parent_id
        if induk_baru is not None:
            if induk_baru not in folders:
                raise HTTPException(status_code=404, detail="Folder induk tidak ditemukan.")
            # Memindahkan folder ke dalam keturunannya sendiri memutus subpohon
            # itu dari akar: ia tidak akan pernah muncul lagi di sidebar dan
            # tidak bisa dipindahkan kembali.
            if induk_baru in _descendant_ids(folders, folder.id):
                raise HTTPException(
                    status_code=400,
                    detail="Folder tidak bisa dipindahkan ke dalam dirinya sendiri.",
                )
            kedalaman = len(_ancestor_ids(folders, induk_baru)) + _subtree_height(folders, folder.id)
            if kedalaman > MAX_FOLDER_DEPTH:
                raise HTTPException(
                    status_code=400,
                    detail=f"Folder maksimal {MAX_FOLDER_DEPTH} tingkat.",
                )
        folder.parent_id = induk_baru

    await db.commit()
    await db.refresh(folder)
    folders[folder.id] = folder
    return _folder_response(folder, folders, await _direct_counts(db, current_user.id))


@router.delete("/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hapus satu folder; isinya dinaikkan ke induk folder tersebut.

    Isi folder sengaja TIDAK ikut terhapus — menghapus wadah tidak boleh
    menghapus draf. Pemindahan anak dilakukan eksplisit di sini karena basis
    data berjalan tanpa penegakan foreign key (lihat app/db/session.py): tanpa
    langkah ini subfolder dan dokumen akan menunjuk id mati dan lenyap dari UI.
    """
    folder = await _get_owned_folder(db, folder_id, current_user)
    induk = folder.parent_id

    await db.execute(
        update(CoWriterFolder)
        .where(
            CoWriterFolder.parent_id == folder.id,
            CoWriterFolder.user_id == current_user.id,
        )
        .values(parent_id=induk)
    )
    await db.execute(
        update(CoWriterDocument)
        .where(
            CoWriterDocument.folder_id == folder.id,
            CoWriterDocument.user_id == current_user.id,
        )
        .values(folder_id=induk)
    )
    await db.delete(folder)
    await db.commit()


@router.put("/documents/{doc_id}/folder", response_model=CoWriterSummaryOut)
async def move_document_to_folder(
    doc_id: uuid.UUID,
    payload: CoWriterMoveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterSummaryOut:
    """Pindahkan draf ke folder lain; ``folder_id: null`` mengeluarkannya ke akar."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    if payload.folder_id is not None:
        await _get_owned_folder(db, payload.folder_id, current_user)
    doc.folder_id = payload.folder_id
    await db.commit()
    await db.refresh(doc)
    return _summary(doc)


# --------------------------------------------------------------------------- #
# CRUD dokumen
# --------------------------------------------------------------------------- #


@router.get("/documents", response_model=CoWriterListOut)
async def list_documents(
    folder_id: str | None = Query(
        None,
        description='Saring per folder: UUID folder (termasuk isi subfoldernya), "root" untuk draf tanpa folder, atau kosong untuk semua',
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterListOut:
    """Daftar draf milik user, terbaru di atas.

    Tanpa ``folder_id`` seluruh draf dikembalikan seperti sebelumnya, jadi
    pemanggil yang sudah ada tidak berubah perilakunya.
    """
    query = select(CoWriterDocument).where(CoWriterDocument.user_id == current_user.id)

    if folder_id == "root":
        query = query.where(CoWriterDocument.folder_id.is_(None))
    elif folder_id:
        try:
            target = uuid.UUID(folder_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="folder_id harus UUID atau \"root\".")
        folders = await _folder_map(db, current_user.id)
        if target not in folders:
            raise HTTPException(status_code=404, detail="Folder tidak ditemukan.")
        # Memilih folder induk juga menampilkan isi subfoldernya; kalau tidak,
        # draf yang tersimpan lebih dalam akan tampak hilang.
        query = query.where(CoWriterDocument.folder_id.in_(_descendant_ids(folders, target)))

    result = await db.scalars(query.order_by(CoWriterDocument.updated_at.desc()))
    return CoWriterListOut(documents=[_summary(doc) for doc in result.all()])


@router.post("/documents", response_model=CoWriterDocumentOut, status_code=status.HTTP_201_CREATED)
async def create_document(
    payload: CoWriterCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterDocumentOut:
    """Buat draf baru; judul diturunkan dari isi bila tidak diberikan."""
    title = (payload.title or "").strip() or _derive_title(payload.content or "")
    if payload.folder_id is not None:
        await _get_owned_folder(db, payload.folder_id, current_user)
    doc = CoWriterDocument(
        user_id=current_user.id,
        folder_id=payload.folder_id,
        title=title,
        content=payload.content or "",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return _detail(doc)


@router.get("/documents/{doc_id}", response_model=CoWriterDocumentOut)
async def get_document(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterDocumentOut:
    doc = await _get_owned_doc(db, doc_id, current_user)
    # Markdown-first: editor bekerja pada Markdown. Draf lama yang tersimpan
    # sebagai LaTeX dikonversi SEKALI saat pertama dibuka; checkpoint dibuat
    # lebih dulu supaya isi LaTeX aslinya bisa dipulihkan bila konversi perlu
    # diperbaiki manual. AST dipakai bila ada (lebih rapi daripada mengurai
    # LaTeX), else jatuh ke latex_to_markdown.
    if doc.content_format != "markdown":
        if (doc.content or "").strip():
            await simpan_checkpoint(db, doc, current_user, "Sebelum konversi ke Markdown")
            markdown = ""
            if doc.structured_content:
                try:
                    from app.services.doc_ast import DocumentAst, ast_to_markdown

                    markdown = ast_to_markdown(DocumentAst.from_json(doc.structured_content))
                except Exception:  # noqa: BLE001 â€” AST rusak â†’ konversi LaTeX
                    markdown = ""
            doc.content = markdown or latex_to_markdown(doc.content)
        doc.content_format = "markdown"
        await db.commit()
        await db.refresh(doc)
    return _detail(doc)


@router.get("/documents/{doc_id}/source")
async def get_document_source(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Kirim berkas upload asli; PDF dipakai untuk pratinjau fidelitas penuh."""
    await _get_owned_doc(db, doc_id, current_user)
    source = _source_file(doc_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Berkas sumber asli tidak tersedia.")
    path, media_type = source
    return FileResponse(path, media_type=media_type)


@router.get("/documents/{doc_id}/working-docx")
async def get_working_docx(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sajikan DOCX kerja (hasil pipeline import pdf2docx/postprocess) sebagai
    dokumen NATIVE untuk editor SuperDoc. Menghindari jalur markdown→DOCX ulang
    yang membuat sintaks Markdown/Pandoc bocor ke editor."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    path = await _prepare_onlyoffice_docx(doc)
    if not path.is_file() or path.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="DOCX kerja belum tersedia.")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"{doc.title or 'dokumen'}.docx",
    )


@router.put("/documents/{doc_id}/working-docx")
async def save_working_docx(
    doc_id: uuid.UUID,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simpan DOCX kerja dari editor SuperDoc (autosave mode Word).

    Inilah SATU-SATUNYA sumber kebenaran mode Word: apa yang tampil di editor =
    apa yang tersimpan di sini = apa yang terunduh saat ekspor. Kolom `doc.sfdt`
    (era Syncfusion) berhenti dipakai — dulu ia menyimpan cap waktu, bukan
    dokumen, sehingga tiap suntingan hilang saat refresh. `doc.content` (buffer
    LaTeX mode Sumber) TIDAK disentuh.
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    contents = await file.read()
    # Tolak berkas kosong / non-DOCX (DOCX = arsip ZIP, magic "PK"). Ini
    # pertahanan langsung terhadap kelas bug "blob kosong menimpa dokumen":
    # bila editor belum siap dan mengekspor blob kosong, jangan sampai ia
    # menghapus dokumen pengguna.
    if len(contents) < 4 or contents[:2] != b"PK":
        raise HTTPException(
            status_code=422,
            detail="Berkas bukan DOCX yang sah (kosong atau bukan arsip ZIP).",
        )

    target = _onlyoffice_docx_path(doc_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Tulis atomik: ke .tmp lalu os.replace. Autosave yang terputus di tengah
    # tidak boleh meninggalkan DOCX rusak yang gagal dimuat editor.
    tmp = target.with_suffix(".docx.tmp")
    from starlette.concurrency import run_in_threadpool

    def _tulis_atomik() -> None:
        tmp.write_bytes(contents)
        os.replace(tmp, target)

    try:
        await run_in_threadpool(_tulis_atomik)
    finally:
        tmp.unlink(missing_ok=True)

    # Sidecar: tandai pengguna sudah menyunting → auto-heal tidak akan menimpa.
    _tulis_sidecar_pipeline(doc_id, user_edited=True)

    doc.updated_at = datetime.utcnow()
    doc.sfdt = ""
    await db.commit()
    return {"ok": True, "bytes": len(contents)}


async def _prepare_onlyoffice_docx(doc: CoWriterDocument) -> Path:
    """Siapkan DOCX kerja. DOCX asli diprioritaskan agar layout tetap utuh."""
    from starlette.concurrency import run_in_threadpool
    from app.services.docx_postprocess import _PIPELINE_IMPOR_VERSI

    target = _onlyoffice_docx_path(doc.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    source = _source_file(doc.id)

    # Auto-heal: dokumen impor-PDF yang dibangun dengan pipeline lama dibangun
    # ulang otomatis — TAPI hanya bila pengguna belum menyuntingnya (sidecar
    # `user_edited`). Setelah mode Word menyimpan, DOCX kerja memuat hasil kerja
    # pengguna, jadi bangun-ulang tanpa syarat ini akan menghapusnya. Sidecar
    # hilang dihitung versi 0 (dokumen lama sebelum fitur ini) → dibangun ulang
    # sekali, lalu sidecar ditulis supaya tidak berulang.
    if source and source[0].suffix.lower() == ".pdf":
        sidecar = _baca_sidecar_pipeline(doc.id)
        if sidecar.get("user_edited") is not True and int(
            sidecar.get("pipeline") or 0
        ) < _PIPELINE_IMPOR_VERSI:
            target.unlink(missing_ok=True)

    if not (target.is_file() and target.stat().st_size > 0):
        if source and source[0].suffix.lower() == ".docx":
            await run_in_threadpool(shutil.copyfile, source[0], target)
        elif source and source[0].suffix.lower() == ".pdf":
            # Sumber PDF WAJIB dikonversi ulang lewat pipeline impor. Tanpa
            # cabang ini alurnya jatuh ke markdown di bawah, dan seluruh layout
            # hasil ekstraksi (posisi baris, tabel, gambar) hilang tanpa jejak.
            await _rebuild_docx_dari_pdf(doc.id, source[0], target)
            # Sidecar: DOCX kerja baru dari pipeline sekarang, belum disunting.
            _tulis_sidecar_pipeline(doc.id, user_edited=False)
        elif doc.sfdt and doc.sfdt.lstrip().startswith("<"):
            from app.services.html_docx_exporter import html_to_docx

            await run_in_threadpool(html_to_docx, doc.sfdt, str(target), doc.title)
        else:
            markdown = ""
            if doc.structured_content:
                try:
                    from app.services.doc_ast import DocumentAst, ast_to_markdown

                    markdown = ast_to_markdown(DocumentAst.from_json(doc.structured_content))
                except Exception:  # noqa: BLE001
                    markdown = ""
            if not markdown:
                markdown = latex_to_markdown(doc.content or "")
            from app.services.docx_template_exporter import markdown_to_docx_template

            await run_in_threadpool(markdown_to_docx_template, markdown, str(target), None)
        # Hapus auto-numbering dari style heading (numPr → 0) supaya teks "1.1 …"
        # tidak dirender dengan nomor ganda ("1.1 1.1 …"). HANYA untuk berkas
        # yang baru dibangun: menjalankannya tiap GET akan menulis ulang berkas
        # hasil suntingan pengguna dan menggeser mtime-nya tiap kali.
        await run_in_threadpool(_strip_heading_numbering, target)
    return target


async def _rebuild_docx_dari_pdf(doc_id: uuid.UUID, pdf_path: Path, target: Path):
    """Konversi PDF sumber → DOCX kerja lewat pipeline impor yang sama.

    Urutannya harus identik dengan cabang `.pdf` di `import_file`: konversi,
    lalu `postprocess_docx` (heading + cover), lalu `post_process_converted_docx`
    (koreksi bold dari font-flags PDF + spacing dot-leader). Melewatkan salah
    satu menghasilkan DOCX yang berbeda dari hasil impor pertama.

    Return `ConversionResult` supaya pemanggil bisa melaporkan metode & durasi.
    """
    from starlette.concurrency import run_in_threadpool

    from app.services.docx_postprocess import post_process_converted_docx, postprocess_docx
    from app.services.pdf_docx_import import convert_pdf_to_docx

    target.parent.mkdir(parents=True, exist_ok=True)
    ok, res = await run_in_threadpool(convert_pdf_to_docx, pdf_path, target)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"Gagal mengonversi PDF ke DOCX. Detail teknis: {res.error}",
        )
    await run_in_threadpool(postprocess_docx, target)
    await run_in_threadpool(post_process_converted_docx, target, pdf_path)
    log_import_pdf_conversion(doc_id, res)
    return res


@router.post("/documents/{doc_id}/rebuild-working-docx")
async def rebuild_working_docx(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bangun ulang DOCX kerja dari PDF asli yang tersimpan.

    Dipakai untuk dokumen yang diimpor sebelum perbaikan pipeline: hasil
    konversinya sudah tersimpan rusak dan tidak akan berubah sendiri karena
    DOCX kerja hanya dibuat sekali.

    `doc.sfdt` dikosongkan supaya editor benar-benar memuat DOCX yang baru —
    frontend memprioritaskan `sfdt` bila ada. Suntingan mode Word karena itu
    hilang, jadi pemanggilnya harus mengonfirmasi lebih dulu. `doc.content`
    (buffer LaTeX) TIDAK disentuh: di situ ada hasil kerja pengguna, dan
    melewatinya juga menghindari langkah pandoc yang lambat.
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    source = _source_file(doc_id)
    if source is None or source[0].suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=400,
            detail="Bangun ulang hanya tersedia untuk dokumen yang diimpor dari PDF.",
        )

    target = _onlyoffice_docx_path(doc_id)
    target.unlink(missing_ok=True)
    try:
        res = await _rebuild_docx_dari_pdf(doc_id, source[0], target)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Gagal membangun ulang: {exc}")

    # Sidecar ditimpa: pipeline sekarang, dan status suntingan direset ke false
    # (bangun-ulang eksplisit membuang DOCX kerja lama beserta suntingannya).
    _tulis_sidecar_pipeline(doc_id, user_edited=False)
    doc.sfdt = ""
    await db.commit()
    return {
        "ok": True,
        "method": getattr(res, "method", None),
        "duration_sec": getattr(res, "duration_sec", None),
    }


def _strip_heading_numbering(path: Path) -> int:
    """Setiap paragraf ber-style Judul*/Heading* diberi numPr numId=0 (tanpa
    penomoran otomatis). Template kampus memakai numId=1 ('Bab %1'/'%1.%2'),
    sehingga heading yang teksnya sudah memuat nomor jadi ganda di editor."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    document = Document(str(path))
    changed = 0
    for p in document.paragraphs:
        try:
            sid = p.style.style_id if p.style else ""
        except Exception:  # noqa: BLE001
            sid = ""
        if not sid or not sid.startswith(("Judul", "Heading")):
            continue
        pPr = p._p.get_or_add_pPr()
        for num_pr in pPr.findall(qn("w:numPr")):
            pPr.remove(num_pr)
        num_pr = OxmlElement("w:numPr")
        num_id = OxmlElement("w:numId")
        num_id.set(qn("w:val"), "0")
        num_pr.append(num_id)
        pPr.append(num_pr)
        changed += 1
    if changed:
        document.save(str(path))
    return changed


def log_import_pdf_conversion(doc_id: uuid.UUID, res) -> None:
    """Catat metode konversi PDF→DOCX yang berhasil (observability)."""
    try:
        detail = getattr(res, "detail", None) or ""
        logging.getLogger("nalar.import.pdf").info(
            "import pdf doc=%s method=%s dur=%ss %s",
            doc_id, getattr(res, "method", "?"),
            getattr(res, "duration_sec", 0), detail,
        )
    except Exception:  # noqa: BLE001 — logging tak boleh menggagalkan impor
        pass


@router.get("/documents/{doc_id}/onlyoffice-config")
async def get_onlyoffice_config(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await _get_owned_doc(db, doc_id, current_user)
    # PDF sumber tidak dibuka langsung sebagai dokumen PDF di editor. Mode PDF
    # membuat halaman tampak seperti gambar dan menyebabkan pengguna tidak
    # dapat menyunting struktur hasil impor. Selalu buat DOCX kerja dari AST
    # hasil ekstraksi PDF agar heading, paragraf, tabel, dan media menjadi
    # elemen dokumen yang benar-benar dapat diedit di ONLYOFFICE.
    path = await _prepare_onlyoffice_docx(doc)
    file_type = "docx"
    document_type = "word"
    token = _onlyoffice_token(doc_id)
    # Document key DETERMINISTIK: hash(doc_id + mtime_ns). Key hanya berubah
    # kalau file DOCX kerja benar-benar ditulis ulang (isi berubah), sehingga
    # Document Server dapat memakai cache render untuk load berikutnya pada
    # dokumen yang sama — mencegah re-render dari nol setiap halaman dibuka.
    version = int(path.stat().st_mtime_ns)
    import hashlib

    doc_key = hashlib.sha256(f"{doc_id}-{version}".encode()).hexdigest()[:40]
    public_backend = settings.backend_base_url_from_docker
    api_path = f"{settings.API_PREFIX}/co_writer"
    return {
        "documentServerUrl": "http://localhost:8090",
        "config": {
            "document": {
                "fileType": file_type,
                "key": doc_key,
                "title": f"{doc.title or 'Dokumen'}.docx",
                "url": f"{public_backend}{api_path}/onlyoffice/file/{doc_id}?token={token}&format={file_type}",
                "permissions": {"edit": True, "download": True, "print": True},
            },
            "documentType": document_type,
            "editorConfig": {
                "callbackUrl": f"{public_backend}{api_path}/onlyoffice/callback/{doc_id}?token={token}&format={file_type}",
                "lang": "id",
                "mode": "edit",
                "user": {
                    "id": str(current_user.id),
                    "name": current_user.full_name or current_user.username,
                },
                "customization": {"autosave": True, "forcesave": True},
            },
            "height": "100%",
            "width": "100%",
        },
    }


@router.get("/onlyoffice/file/{doc_id}")
async def get_onlyoffice_file(
    doc_id: uuid.UUID,
    token: str = Query(...),
    format: str = Query(default="docx"),
):
    _verify_onlyoffice_token(doc_id, token)
    is_pdf = format.lower() == "pdf"
    path = _onlyoffice_pdf_path(doc_id) if is_pdf else _onlyoffice_docx_path(doc_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Dokumen ONLYOFFICE belum tersedia.")
    return FileResponse(
        path,
        media_type="application/pdf" if is_pdf else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="document.pdf" if is_pdf else "document.docx",
    )


@router.post("/onlyoffice/callback/{doc_id}")
async def onlyoffice_callback(
    doc_id: uuid.UUID,
    payload: dict,
    token: str = Query(...),
    format: str = Query(default="docx"),
    db: AsyncSession = Depends(get_db),
):
    _verify_onlyoffice_token(doc_id, token)
    status_value = int(payload.get("status") or 0)
    if status_value in (2, 3, 6, 7) and payload.get("url"):
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(str(payload["url"]))
            response.raise_for_status()
            contents = response.content
        is_pdf = format.lower() == "pdf"
        target = _onlyoffice_pdf_path(doc_id) if is_pdf else _onlyoffice_docx_path(doc_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        if not is_pdf:
            # ONLYOFFICE menyimpan hasil suntingan pengguna → tandai supaya
            # auto-heal tidak menimpanya dengan hasil bangun-ulang dari PDF.
            _tulis_sidecar_pipeline(doc_id, user_edited=True)
        doc = await db.scalar(select(CoWriterDocument).where(CoWriterDocument.id == doc_id))
        if doc is not None and not is_pdf:
            from starlette.concurrency import run_in_threadpool
            from app.services.sfdt_pipeline import docx_to_latex

            doc.content = await run_in_threadpool(docx_to_latex, contents, title=doc.title or "")
            doc.content_format = "latex"
            doc.sfdt = None
            await db.commit()
    return {"error": 0}


@router.post("/documents/{doc_id}/export-pdf")
async def export_pdf(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ekspor PDF mode Word — cetak DOCX kerja apa adanya lewat LibreOffice.

    "Persis seperti di layar": editor menyunting DOCX kerja, jadi PDF-nya
    dicetak dari DOCX kerja yang sama. TIDAK ada pintasan menyalin
    `source/original.pdf` (itu membuang seluruh suntingan pengguna), TIDAK ada
    ONLYOFFICE (servisnya mati), dan TIDAK ada fallback Chromium (margin-nya
    dipaku ke template kampus — hasilnya bukan "seperti di layar").
    """
    from starlette.concurrency import run_in_threadpool
    from app.services.docx_pdf_export import docx_to_pdf, tersedia

    doc = await _get_owned_doc(db, doc_id, current_user)
    if not tersedia():
        raise HTTPException(
            status_code=503,
            detail="Ekspor PDF butuh LibreOffice yang belum terpasang di peladen ini.",
        )

    docx_path = await _prepare_onlyoffice_docx(doc)
    if not docx_path.is_file() or docx_path.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="DOCX kerja belum tersedia.")

    pdf_path = _onlyoffice_pdf_path(doc_id)
    ok, pesan = await run_in_threadpool(docx_to_pdf, docx_path, pdf_path)
    if not ok:
        raise HTTPException(status_code=502, detail=f"Gagal mencetak PDF: {pesan}")

    filename = re.sub(r"[^\w .-]+", "_", doc.title or "dokumen").strip() or "dokumen"
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{filename}.pdf",
    )


@router.put("/documents/{doc_id}", response_model=CoWriterDocumentOut)
async def update_document(
    doc_id: uuid.UUID,
    payload: CoWriterUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterDocumentOut:
    """Perbarui draf (auto-save memanggil endpoint ini). null = tidak diubah."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    changed = False
    if payload.content is not None and payload.content != doc.content:
        doc.content = payload.content
        changed = True
    if payload.title is not None and payload.title != doc.title:
        doc.title = payload.title
        changed = True
    if changed:
        await db.commit()
        await db.refresh(doc)
    return _detail(doc)


@router.delete("/documents/{doc_id}", response_model=CoWriterDeletedOut)
async def delete_document(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterDeletedOut:
    doc = await _get_owned_doc(db, doc_id, current_user)
    # Tabel anak dihapus eksplisit, bukan mengandalkan ON DELETE CASCADE:
    # SQLite mengabaikan kunci asing kecuali `PRAGMA foreign_keys=ON` disetel per
    # koneksi, dan itu tidak disetel — akibatnya baris berkas dan checkpoint
    # tertinggal sebagai yatim setiap kali draf dihapus.
    from app.models.co_writer_checkpoint import CoWriterCheckpoint

    await db.execute(delete(CoWriterFile).where(CoWriterFile.doc_id == doc_id))
    await db.execute(delete(CoWriterCheckpoint).where(CoWriterCheckpoint.doc_id == doc_id))
    await db.delete(doc)
    await db.commit()
    shutil.rmtree(_document_upload_dir(doc_id), ignore_errors=True)
    return CoWriterDeletedOut(deleted=True)


@router.get("/documents/{doc_id}/md")
async def get_document_markdown(
    doc_id: uuid.UUID,
    path: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Konversi LaTeX â†’ Markdown untuk mode edit ala Word.

    LaTeX tetap satu-satunya sumber kebenaran; Markdown ini hanyalah tampilan
    yang bisa diedit pengguna. Tanpa `path` yang dibaca main.tex; dengan `path`
    (mis. bab/01.tex) yang dibaca berkas anak itu.
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    # `path` bisa berupa objek Query bila rute dipanggil langsung (uji unit).
    if isinstance(path, str) and path:
        try:
            jalur = bersihkan_jalur(path)
        except JalurTidakSah as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        berkas = await _get_file(db, doc_id, jalur)
        markdown = latex_to_markdown(berkas.content or "")
        return {
            "markdown": markdown,
            "title": doc.title,
            "updated_at": _epoch(berkas.updated_at),
        }
    # AST-first: dokumen dengan struktur tersimpan dirender dari AST (struktur
    # kanonik PRD: judul cover tunggal, heading terpisah). Markdown-first:
    # dokumen yang sudah bermformat markdown dikembalikan apa adanya (tanpa
    # round-trip). Fallback ke konversi LaTeX untuk draf lama.
    if doc.content_format == "markdown":
        return {
            "markdown": doc.content or "",
            "title": doc.title,
            "updated_at": _epoch(doc.updated_at),
        }
    if doc.structured_content:
        try:
            from app.services.doc_ast import DocumentAst, ast_to_markdown

            markdown = ast_to_markdown(DocumentAst.from_json(doc.structured_content))
            return {
                "markdown": markdown,
                "title": doc.title,
                "updated_at": _epoch(doc.updated_at),
            }
        except Exception:  # noqa: BLE001 â€” AST rusak â†’ pakai konversi LaTeX
            pass
    markdown = latex_to_markdown(doc.content or "")
    return {
        "markdown": markdown,
        "title": doc.title,
        "updated_at": _epoch(doc.updated_at),
    }


@router.post("/documents/{doc_id}/from-md")
async def save_document_from_markdown(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simpan hasil edit dari editor markdown.

    Judul dokumen sengaja TIDAK disentuh â€” mengubah isi tidak boleh mengubah
    judul TA. Bila `path` diberikan, hasilnya ditulis ke berkas anak proyek
    LaTeX (mis. bab/01.tex) dan main.tex tidak tersentuh; jalur berkas-anak itu
    adalah fitur proyek multi-berkas LaTeX terpisah dan tetap memakai LaTeX.
    """
    await _get_owned_doc(db, doc_id, current_user)
    isi = (payload or {}).get("markdown")
    if not isinstance(isi, str):
        raise HTTPException(status_code=422, detail="markdown harus berupa teks.")

    jalur_mentah = (payload or {}).get("path")
    if jalur_mentah not in (None, ""):
        latex = markdown_to_latex(isi, preserve_source=True)
        try:
            jalur = bersihkan_jalur(str(jalur_mentah))
        except JalurTidakSah as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        berkas = await db.scalar(
            select(CoWriterFile).where(
                CoWriterFile.doc_id == doc_id,
                CoWriterFile.path == jalur,
            )
        )
        if berkas is None:
            berkas = CoWriterFile(
                doc_id=doc_id,
                user_id=current_user.id,
                path=jalur,
                content=latex,
            )
            db.add(berkas)
        else:
            berkas.content = latex
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Jalur berkas sudah digunakan.")
        await db.refresh(berkas)
        return {
            "path": berkas.path,
            "content": berkas.content or "",
            "updated_at": _epoch(berkas.updated_at),
        }

    # Markdown-first: dokumen utama disimpan sebagai Markdown apa adanya.
    doc = await _get_owned_doc(db, doc_id, current_user)
    doc.content = isi
    doc.content_format = "markdown"
    # AST lama tidak lagi selaras dengan isi baru â€” invalidasi supaya GET /md
    # (yang AST-first untuk doc lama) langsung merefleksikan hasil edit.
    doc.structured_content = None
    await db.commit()
    await db.refresh(doc)
    return {
        "content": doc.content or "",
        "title": doc.title,
        "updated_at": _epoch(doc.updated_at),
    }


@router.get("/documents/{doc_id}/sfdt")
async def get_document_sfdt(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SFDT (JSON Syncfusion Document Editor) â€” representasi kerja editor ala Word.

    Kosong bila dokumen belum pernah dibuka/dikonversi di editor baru; klien
    lalu memulai dari dokumen kosong atau hasil impor DOCX.
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    return {
        "sfdt": doc.sfdt or "",
        "title": doc.title,
        "updated_at": _epoch(doc.updated_at),
    }


@router.post("/documents/{doc_id}/sfdt")
async def save_document_sfdt(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simpan SFDT hasil edit editor ala Word (sync tanpa konversi LaTeX).

    Konten LaTeX/AST lama sengaja dibiarkan utuh â€” pipeline ekspor dan
    pratinjau lama tetap membaca itu sampai jalur ekspor baru (SFDT â†’ DOCX â†’
    Markdown â†’ Pandoc) selesai dibangun.
    """
    await _get_owned_doc(db, doc_id, current_user)
    sfdt = (payload or {}).get("sfdt")
    if not isinstance(sfdt, str) or not sfdt.strip():
        raise HTTPException(status_code=422, detail="sfdt harus berupa teks JSON.")
    doc = await _get_owned_doc(db, doc_id, current_user)
    doc.sfdt = sfdt
    await db.commit()
    await db.refresh(doc)
    return {"updated_at": _epoch(doc.updated_at)}


@router.get("/documents/{doc_id}/files")
async def list_document_files(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daftar metadata berkas anak; isi sengaja tidak dikirim."""
    await _get_owned_doc(db, doc_id, current_user)
    berkas = (
        await db.scalars(
            select(CoWriterFile)
            .where(CoWriterFile.doc_id == doc_id)
            .order_by(CoWriterFile.path.asc())
        )
    ).all()
    hasil = [_ringkas_berkas(item) for item in berkas]

    # Folder gambar adalah tampilan baca-saja atas hasil impor yang memang
    # sudah disimpan di disk. Gambar tidak boleh masuk kolom Text pada tabel
    # co_writer_files, tetapi tetap perlu muncul di pohon proyek.
    direktori_gambar = Path("uploads") / str(doc_id) / "images"
    if direktori_gambar.is_dir():
        for gambar in sorted(direktori_gambar.iterdir(), key=lambda item: item.name.lower()):
            if not gambar.is_file():
                continue
            statistik = gambar.stat()
            hasil.append(
                {
                    "path": f"gambar/{gambar.name}",
                    "size": statistik.st_size,
                    "updated_at": int(statistik.st_mtime),
                    "read_only": True,
                    "url": (
                        f"/api/v1/co_writer/documents/{doc_id}/images/"
                        f"{quote(gambar.name)}"
                    ),
                }
            )
    hasil.sort(key=lambda item: item["path"].lower())
    return {"files": hasil}


@router.get("/documents/{doc_id}/outline")
async def get_document_outline(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Outline seluruh proyek dengan lokasi yang dapat dibuka oleh editor."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    child_files = await _muat_berkas(db, doc.id)
    sources = {"main.tex": _sumber_tex(doc)}
    sources.update(
        (path, content)
        for path, content in sorted(child_files.items())
        if path.lower().endswith((".tex", ".md"))
    )
    headings = _ekstrak_outline(sources)
    return {"headings": headings, "total": len(headings)}


@router.get("/documents/{doc_id}/images/{path:path}")
async def get_document_image(
    doc_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Buka gambar impor melalui jalur API yang diproksi frontend."""
    await _get_owned_doc(db, doc_id, current_user)
    try:
        jalur = bersihkan_jalur(path)
    except JalurTidakSah as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if "/" in jalur:
        raise HTTPException(status_code=422, detail="Nama gambar tidak sah.")
    gambar = Path("uploads") / str(doc_id) / "images" / jalur
    if not gambar.is_file():
        raise HTTPException(status_code=404, detail="Gambar tidak ditemukan.")
    return FileResponse(gambar)


@router.get("/documents/{doc_id}/files/{path:path}")
async def get_document_file(
    doc_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_doc(db, doc_id, current_user)
    try:
        jalur = bersihkan_jalur(path)
    except JalurTidakSah as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    berkas = await _get_file(db, doc_id, jalur)
    return {
        "path": berkas.path,
        "content": berkas.content or "",
        "updated_at": _epoch(berkas.updated_at),
    }


@router.put("/documents/{doc_id}/files/{path:path}")
async def save_document_file(
    doc_id: uuid.UUID,
    path: str,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_doc(db, doc_id, current_user)
    try:
        jalur = bersihkan_jalur(path)
    except JalurTidakSah as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    isi = (payload or {}).get("content")
    if not isinstance(isi, str):
        raise HTTPException(status_code=422, detail="content harus berupa teks.")

    berkas = await db.scalar(
        select(CoWriterFile).where(
            CoWriterFile.doc_id == doc_id,
            CoWriterFile.path == jalur,
        )
    )
    if berkas is None:
        berkas = CoWriterFile(
            doc_id=doc_id,
            user_id=current_user.id,
            path=jalur,
            content=isi,
        )
        db.add(berkas)
    else:
        berkas.content = isi

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Jalur berkas sudah digunakan.")
    await db.refresh(berkas)
    return {
        "path": berkas.path,
        "content": berkas.content or "",
        "updated_at": _epoch(berkas.updated_at),
    }


@router.delete("/documents/{doc_id}/files/{path:path}")
async def delete_document_file(
    doc_id: uuid.UUID,
    path: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_doc(db, doc_id, current_user)
    try:
        jalur = bersihkan_jalur(path)
    except JalurTidakSah as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    berkas = await _get_file(db, doc_id, jalur)
    await db.delete(berkas)
    await db.commit()
    return {"deleted": True, "path": jalur}


@router.post("/documents/{doc_id}/files/rename")
async def rename_document_file(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_owned_doc(db, doc_id, current_user)
    asal_mentah = (payload or {}).get("from")
    tujuan_mentah = (payload or {}).get("to")
    if not isinstance(asal_mentah, str) or not isinstance(tujuan_mentah, str):
        raise HTTPException(status_code=422, detail="from dan to harus berupa teks.")
    try:
        asal = bersihkan_jalur(asal_mentah)
        tujuan = bersihkan_jalur(tujuan_mentah)
    except JalurTidakSah as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    berkas = await _get_file(db, doc_id, asal)
    if asal != tujuan:
        sudah_ada = await db.scalar(
            select(CoWriterFile.id).where(
                CoWriterFile.doc_id == doc_id,
                CoWriterFile.path == tujuan,
            )
        )
        if sudah_ada is not None:
            raise HTTPException(status_code=409, detail="Jalur tujuan sudah digunakan.")
        berkas.path = tujuan
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Jalur tujuan sudah digunakan.")
        await db.refresh(berkas)
    return {
        "path": berkas.path,
        "content": berkas.content or "",
        "updated_at": _epoch(berkas.updated_at),
    }


@router.post("/documents/{doc_id}/split")
async def split_document(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pecah ``main.tex`` per section setelah menyimpan checkpoint."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    berkas_bab = (
        await db.scalars(
            select(CoWriterFile).where(
                CoWriterFile.doc_id == doc_id,
                CoWriterFile.path.like("bab/%"),
            )
        )
    ).all()
    if berkas_bab:
        raise HTTPException(
            status_code=409,
            detail="Folder bab sudah berisi berkas; pemecahan dibatalkan.",
        )

    try:
        utama, hasil_bab = _pecah_per_bab(await _sumber_tex_proyek(db, doc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    checkpoint = await simpan_checkpoint(
        db, doc, current_user, "Sebelum pecah per bab"
    )
    doc.content = utama
    doc.content_format = "latex"
    dibuat: list[CoWriterFile] = []
    for jalur, isi in hasil_bab:
        item = CoWriterFile(
            doc_id=doc.id,
            user_id=current_user.id,
            path=jalur,
            content=isi,
        )
        db.add(item)
        dibuat.append(item)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Salah satu jalur bab sudah digunakan; pemecahan dibatalkan.",
        )
    await db.refresh(doc)
    await db.refresh(checkpoint)
    for item in dibuat:
        await db.refresh(item)
    return {
        "content": doc.content,
        "checkpoint_id": str(checkpoint.id),
        "files": [_ringkas_berkas(item) for item in dibuat],
    }


@router.get("/documents/{doc_id}/export-docx")
async def export_document_docx(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unduh draf Co-Writer sebagai DOCX dengan sitasi [n] jadi hyperlink DOI aktif."""
    doc = await _get_owned_doc(db, doc_id, current_user)

    # Jembatan: draf disimpan sebagai LaTeX murni, tapi pengekspor DOCX
    # membaca Markdown. Konversi on-the-fly tanpa mengubah penyimpanan.
    from app.services.latex_export import latex_to_markdown
    isi = doc.content or ""
    if doc.content_format == "latex":
        # Didatarkan dulu: tanpa ini DOCX proyek multi-berkas hanya berisi
        # preamble dan daftar \input, bukan naskahnya.
        tex_source = await _sumber_tex_proyek(db, doc)
        isi = latex_to_markdown(
            localize_latex_image_paths(
                tex_source,
                [str(_document_upload_dir(doc.id) / "images")],
            )
        )

    try:
        output_path, filename = await _docx_file_dari_markdown(
            db, current_user, isi, doc.title or "Draf"
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gagal membuat berkas Word: {exc}")

    return FileResponse(
        path=output_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# --------------------------------------------------------------------------- #
# P1 — pipeline ekspor berbasis SFDT (SFDT → DOCX → Markdown → Pandoc)
# --------------------------------------------------------------------------- #


@router.post("/documents/{doc_id}/convert-docx")
async def convert_docx_to_markdown(
    doc_id: uuid.UUID,
    file: UploadFile = File(...),
    to: str = Query(default="markdown", pattern="^(markdown|latex)$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """P1: DOCX (dari Syncfusion) → Markdown/LaTeX via Pandoc.

    DOCX dihasilkan editor ala Word di browser; konversi balik ini membuat
    Markdown menjadi format netral untuk semua jalur ekspor lama.
    """
    await _get_owned_doc(db, doc_id, current_user)
    docx_bytes = await file.read()
    if not docx_bytes:
        raise HTTPException(status_code=422, detail="Berkas DOCX kosong.")
    try:
        from app.services import sfdt_pipeline

        if to == "latex":
            return {"latex": sfdt_pipeline.docx_to_latex(docx_bytes)}
        return {"markdown": sfdt_pipeline.docx_to_markdown(docx_bytes)}
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/documents/{doc_id}/export-docx")
async def export_docx_from_markdown(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """P1: ekspor DOCX langsung dari markdown (mode Sync — hasil pipeline SFDT)."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    markdown = (payload or {}).get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise HTTPException(status_code=422, detail="markdown harus berupa teks.")
    output_path, filename = await _docx_file_dari_markdown(
        db, current_user, markdown, doc.title or "Draf"
    )
    return FileResponse(
        path=output_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@router.post("/documents/{doc_id}/export-latex")
async def export_latex_from_markdown(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """P1: ekspor LaTeX/PDF dari markdown (mode Sync — hasil pipeline SFDT).

    Body: {"markdown": "...", "format": "pdf"|"tex"}.
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    data = payload or {}
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise HTTPException(status_code=422, detail="markdown harus berupa teks.")
    export_format = str(data.get("format") or "pdf")
    if export_format not in ("pdf", "tex"):
        raise HTTPException(status_code=422, detail="format harus pdf atau tex.")

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", doc.title or "Draf").strip()[:80] or "Draf"

    if export_format == "tex":
        from app.services import pandoc_latex

        try:
            tex_source = pandoc_latex.markdown_to_latex_pandoc(
                markdown, title=doc.title or ""
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        import os
        import tempfile

        tmpdir = tempfile.mkdtemp()
        tex_path = os.path.join(tmpdir, f"{safe_title}.tex")
        with open(tex_path, "w", encoding="utf-8") as fh:
            fh.write(tex_source)
        return FileResponse(
            path=tex_path,
            filename=f"{safe_title}.tex",
            media_type="application/x-tex",
        )

    images_dir = str(_document_upload_dir(doc.id) / "images")
    fname = f"{safe_title.replace(' ', '_')}_{uuid.uuid4().hex[:6]}"
    return await _file_pdf_dari_markdown(
        db, current_user, markdown, doc.title or "Draf",
        images_dir=images_dir, fname=fname,
    )


# --------------------------------------------------------------------------- #
# AI edit draf penuh & automark
# --------------------------------------------------------------------------- #


@router.post("/edit", response_model=CoWriterEditResponse)
async def edit_document(
    payload: CoWriterEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterEditResponse:
    """Tulis ulang / ringkas / kembangkan seluruh draf dengan LLM."""
    llm = await _resolve_llm(db, current_user.id)
    query = payload.instruction.strip() or payload.text[:200]

    context = ""
    if payload.source == "web":
        context = await gather_web_context(query, _noop_trace)
    elif payload.source == "rag":
        embedding = await _try_embedding(db, current_user.id)
        context = await gather_rag_context(
            db, current_user, llm, embedding, query, payload.kb_name, _noop_trace
        )

    writer = CoWriterLLM(llm)
    system, prompt = build_full_edit_prompt(payload.text, payload.instruction, payload.action, context)
    try:
        edited = await asyncio.wait_for(
            writer.ask(prompt, system=system),
            timeout=CO_WRITER_REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail=_ai_timeout_detail()) from exc
    except Exception as exc:
        logger.exception("co_writer edit gagal")
        raise HTTPException(status_code=502, detail=f"AI gagal: {exc}") from exc
    # Jaring pengaman: model kadang tetap balas Markdown walau diminta LaTeX.
    edited = pastikan_latex(edited)
    return CoWriterEditResponse(edited_text=edited)


@router.post("/automark", response_model=CoWriterAutoMarkResponse)
async def auto_mark_document(
    payload: CoWriterAutoMarkRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterAutoMarkResponse:
    """Beri struktur LaTeX (section, tebal, daftar) tanpa mengubah isi kalimat."""
    llm = await _resolve_llm(db, current_user.id)
    writer = CoWriterLLM(llm)
    system, prompt = build_automark_prompt(payload.text)
    try:
        marked = await asyncio.wait_for(
            writer.ask(prompt, system=system, temperature=0.2),
            timeout=CO_WRITER_REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail=_ai_timeout_detail()) from exc
    except Exception as exc:
        logger.exception("co_writer automark gagal")
        raise HTTPException(status_code=502, detail=f"AI gagal: {exc}") from exc
    marked = pastikan_latex(marked)
    return CoWriterAutoMarkResponse(marked_text=marked)


# --------------------------------------------------------------------------- #
# AI edit seleksi â€” streaming SSE
# --------------------------------------------------------------------------- #


@router.post("/edit_react/stream")
async def stream_edit_selection(
    payload: CoWriterStreamEditRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Edit teks terpilih dengan tool opsional (rag/web), hasil dikirim via SSE."""
    llm = await _resolve_llm(db, current_user.id)
    embedding = await _try_embedding(db, current_user.id)
    tools = [t for t in (payload.tools or []) if t in ("rag", "web")]
    query = payload.instruction.strip() or payload.selected_text[:200]

    async def event_stream():
        trace_events: list[tuple[str, dict]] = []

        async def trace(event_type: str, content: str, metadata: dict, *, success: bool = True, result: str = "") -> None:
            if event_type == "tool_call":
                trace_events.append(
                    ("stream", {"type": "tool_call", "content": content, "metadata": metadata})
                )
            else:
                trace_events.append(
                    (
                        "stream",
                        {
                            "type": "tool_result",
                            "content": result,
                            "metadata": {**metadata, "success": success},
                        },
                    )
                )

        try:
            context_parts: list[str] = []
            if "web" in tools:
                web_ctx = await gather_web_context(query, trace)
                if web_ctx:
                    context_parts.append(web_ctx)
            if "rag" in tools:
                rag_ctx = await gather_rag_context(
                    db, current_user, llm, embedding, query, payload.kb_name, trace
                )
                if rag_ctx:
                    context_parts.append(rag_ctx)
            context = "\n\n".join(context_parts)

            # Kirim trace tool setelah semua tool selesai, sebelum token pertama.
            for event_name, event_payload in trace_events:
                yield _sse(event_name, event_payload)

            writer = CoWriterLLM(llm)
            system, prompt = build_selection_prompt(
                payload.selected_text, payload.instruction, payload.mode, context
            )
            full = ""
            async with asyncio.timeout(CO_WRITER_REQUEST_TIMEOUT_SECONDS):
                async for delta in writer.ask_stream(prompt, system=system):
                    full += delta
                    yield _sse("content", {"type": "content", "stage": "responding", "content": delta})

            # Potongan yang dialirkan tampil apa adanya; hasil akhir yang
            # diterapkan ke draf dinormalisasi sekali di sini.
            yield _sse("result", {"edited_text": pastikan_latex(full)})
        except asyncio.TimeoutError:
            yield _sse("error", {"detail": _ai_timeout_detail()})
        except Exception as exc:  # noqa: BLE001 â€” error dikirim sebagai event SSE
            logger.exception("co_writer stream edit gagal")
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# Agentic write & integrasi Ruang Riset
# --------------------------------------------------------------------------- #


@router.post("/agent-write", response_model=AgenticWriteResponse)
async def agentic_write_endpoint(
    payload: AgenticWriteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AgenticWriteResponse:
    """AI membaca seluruh referensi grup, menulis draf, dan menyisipkan sitasi.

    Hasil dikembalikan sebagai DRAF â€” user MENYETUJUI dulu sebelum diterapkan
    ke dokumen ("mau langsung dituliskan ke dokumen atau tidak?").
    """
    llm = await _resolve_llm(db, current_user.id)
    rag_context = ""
    if payload.use_rag:
        embedding = await _try_embedding(db, current_user.id)
        rag_context = await gather_rag_context(
            db, current_user, llm, embedding, payload.instruction, None, _noop_trace
        )
    try:
        result = await agentic_write(
            db,
            current_user,
            llm,
            group_id=payload.group_id,
            instruction=payload.instruction,
            format_name=payload.format,
            rag_context=rag_context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return AgenticWriteResponse(**result)


@router.post("/agent-write/stream")
async def agentic_write_stream_endpoint(
    payload: AgenticWriteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Agentic write streaming (SSE): user melihat AI mengetik real-time.

    Event:
        event: stage
        data: {"stage": "reading", "label": "Membaca jurnalâ€¦"}

        event: content
        data: {"delta": "..."}  # potongan teks yang sedang diketik

        event: result
        data: {"draft": "...", "references": [...], "citation_count": n}
    """
    llm = await _resolve_llm(db, current_user.id)
    rag_context = ""
    if payload.use_rag:
        embedding = await _try_embedding(db, current_user.id)
        rag_context = await gather_rag_context(
            db, current_user, llm, embedding, payload.instruction, None, _noop_trace
        )

    from app.services.agentic_writer import agentic_write_stream

    async def event_stream():
        try:
            yield _sse("stage", {"stage": "reading", "label": "Membaca jurnal dari Ruang Riset…"})
            async for event in agentic_write_stream(
                db,
                current_user,
                llm,
                group_id=payload.group_id,
                instruction=payload.instruction,
                format_name=payload.format,
                rag_context=rag_context,
            ):
                if event["stage"] == "writing":
                    yield _sse("content", {"delta": event["delta"]})
                else:
                    yield _sse("result", {k: v for k, v in event.items()})
        except ValueError as exc:
            yield _sse("error", {"detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent-write stream gagal")
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/documents/{doc_id}/agent-run/stream")
async def agent_run_stream_endpoint(
    doc_id: uuid.UUID,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Loop eksekusi agentic (Fase A / vibe-writing) — SSE.

    Body:
        instruction: str            perintah tunggal pengguna (wajib)
        mode: "cepat"|"seimbang"|"menyeluruh"  (default "seimbang")
        doc_context: str            potret dokumen dari editor (opsional; bila
                                    kosong dipakai sumber proyek di server)
        selection_text: str         teks yang sedang disorot pengguna (opsional)
        model: {...}                pilihan model (opsional)
        phase: "propose"|"execute"  fase alur "rencana → setujui → kerjakan".
                                    propose = usul rencana + brainstorm lalu
                                    berhenti (dokumen tak disentuh); execute =
                                    kerjakan rencana yang disetujui. Default
                                    "execute" (kompatibel pemanggil lama).
        tasks: [str]                judul tugas yang disetujui pengguna; dipakai
                                    saat phase="execute" agar planning dilewati.
        template_context: str       template/contoh dari pengguna (slot khusus)
                                    yang WAJIB diikuti agent: struktur, urutan &
                                    penomoran bab, gaya. Opsional.
        session_memory: str         ringkasan run sebelumnya di dokumen ini
                                    (kontinuitas "lanjutkan setelah gagal";
                                    dibangun FE dari riwayat lokal). Opsional.

    Event SSE (name → data):
        plan {tasks:[{index,title,status}]}
        task_status {index,status,note?}
        tool_call {id,name,args,fe}   fe=true → frontend eksekusi ke editor
        tool_result {id,name,ok,summary}
        text {delta}                  ringkasan akhir yang mengalir
        reasoning {delta}
        usage {...}
        error {detail}
        end {}
    """
    from openai import AsyncOpenAI

    instruction = str(payload.get("instruction", "")).strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="Instruksi kosong.")
    mode = str(payload.get("mode") or "seimbang")
    selection_text = payload.get("selection_text") or None
    extra_context = payload.get("extra_context") or None

    # Fase alur "rencana → setujui → kerjakan" (lihat docstring). Default
    # "execute" + tasks=None menjaga perilaku lama untuk pemanggil yang belum
    # mengirim `phase`.
    phase = str(payload.get("phase") or "execute").strip().lower()
    if phase not in ("propose", "execute"):
        phase = "execute"
    approved_tasks = payload.get("tasks")
    if isinstance(approved_tasks, list):
        approved_tasks = [str(t).strip() for t in approved_tasks if str(t).strip()]
        approved_tasks = approved_tasks or None
    else:
        approved_tasks = None

    # Slot Template khusus (panel co-writer) = kerangka WAJIB diikuti agent.
    # Memori sesi = ringkasan run sebelumnya di dokumen ini (dibangun FE dari
    # riwayat lokal per-dokumen) untuk kontinuitas "lanjutkan setelah gagal".
    # Keduanya opsional & di-inject sebagai teks konteks (desain tetap stateless).
    template_context = payload.get("template_context") or None
    session_memory = payload.get("session_memory") or None

    doc = await _get_owned_doc(db, doc_id, current_user)
    llm = await _resolve_llm(db, current_user.id, payload.get("model"))

    # Konteks dokumen: utamakan potret dari editor (paling mutakhir); bila FE
    # tak mengirim, jatuh ke sumber proyek di server (tanpa konversi DOCX/rebuild).
    doc_context = str(payload.get("doc_context") or "")
    if not doc_context.strip():
        try:
            doc_context = await _sumber_tex_proyek(db, doc)
        except Exception:  # noqa: BLE001
            doc_context = ""

    # Riset web otonom hanya untuk model ber-tier agentic (hindari loop liar pada
    # model lemah); tool tulis tetap tersedia di semua tier.
    allow_web = llm.capability_tier in (
        "agentic_dasar_terverifikasi",
        "agentic_penuh_terverifikasi",
    )

    client = AsyncOpenAI(
        base_url=llm.base_url,
        api_key=llm.api_key or "dummy",
        timeout=CO_WRITER_REQUEST_TIMEOUT_SECONDS,
    )

    async def event_stream():
        try:
            async for line in run_agent_stream(
                client,
                llm.model_name,
                instruction=instruction,
                doc_context=doc_context,
                db=db,
                user_id=current_user.id,
                mode=mode,
                allow_web=allow_web,
                context_window=llm.context_window,
                selection_text=selection_text,
                extra_context=extra_context,
                phase=phase,
                approved_tasks=approved_tasks,
                template_context=template_context,
                session_memory=session_memory,
            ):
                try:
                    evt = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                name = evt.get("event", "message")
                data = evt.get("data", {})
                if not isinstance(data, dict):
                    data = {"value": data}
                yield _sse(name, data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent-run stream gagal")
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------- #
# PRD v2.3: regenerate-bibliography, export pdf/docx, generate-diagram, insert-media
# --------------------------------------------------------------------------- #


@router.post("/documents/{doc_id}/chat")
async def chat_with_document(
    doc_id: uuid.UUID,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Chat research partner dengan konteks dokumen + referensi grup (PRD v2.3).

    Body: {"message": "pertanyaan user"}
    AI membaca seluruh isi dokumen aktif + referensi (RAG) â†’ menjawab.
    """
    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(status_code=422, detail="Pesan kosong.")

    raw_images = payload.get("images") or []
    if not isinstance(raw_images, list):
        raise HTTPException(status_code=422, detail="images harus berupa daftar.")
    images = [str(item) for item in raw_images if isinstance(item, str)][:2]
    if any(
        not image.startswith(("data:image/png;base64,", "data:image/jpeg;base64,"))
        or len(image) > 6_000_000
        for image in images
    ):
        raise HTTPException(
            status_code=422,
            detail="Gambar harus PNG/JPEG dan berukuran maksimal sekitar 4 MB.",
        )

    doc = await _get_owned_doc(db, doc_id, current_user)
    llm = await _resolve_llm(db, current_user.id)
    if images and "vision" not in set(llm.capabilities or []):
        raise HTTPException(
            status_code=422,
            detail="Model aktif belum mendukung gambar. Pilih model dengan kemampuan vision.",
        )

    # Model kecil lebih terbantu oleh konteks yang relevan daripada potongan awal
    # dokumen. Pilih bab dan referensi secara deterministik sebelum memanggil LLM.
    context_options = payload.get("context") or {}
    if not isinstance(context_options, dict):
        raise HTTPException(status_code=422, detail="context harus berupa objek.")
    include_document = context_options.get("document", True) is not False
    include_references = context_options.get("references", True) is not False
    web_mode = str(context_options.get("web", "auto")).lower()
    if web_mode not in {"auto", "on", "off"}:
        raise HTTPException(status_code=422, detail="context.web harus auto, on, atau off.")

    requested_mode = str(payload.get("mode") or "auto").lower()
    allowed_modes = {
        "question",
        "drafting",
        "critique",
        "planning",
        "methodology",
        "literature",
    }
    if requested_mode != "auto" and requested_mode not in allowed_modes:
        raise HTTPException(status_code=422, detail="Mode chat tidak dikenal.")

    child_files = await _muat_berkas(db, doc.id)
    project_files = {"main.tex": _sumber_tex(doc), **child_files}
    mode = classify_research_mode(message) if requested_mode == "auto" else requested_mode
    context_budget = input_char_budget(message, mode, llm.context_window)
    doc_evidence = (
        select_document_evidence(
            message,
            project_files,
            char_budget=max(4500, int(context_budget * 0.62)),
        )
        if include_document
        else DocumentEvidence("(konteks dokumen dinonaktifkan)", [])
    )

    from app.models.journal import JournalReference

    refs = (
        await db.scalars(
            select(JournalReference).where(JournalReference.user_id == current_user.id)
        )
    ).all()
    ref_evidence = (
        select_reference_evidence(
            message,
            refs,
            char_budget=max(1400, int(context_budget * 0.24)),
        )
        if include_references
        else ReferenceEvidence("(konteks referensi dinonaktifkan)", [])
    )

    raw_history = payload.get("history") or []
    if not isinstance(raw_history, list):
        raise HTTPException(status_code=422, detail="history harus berupa daftar.")
    history_context = format_history(
        [item for item in raw_history if isinstance(item, dict)],
        max_chars=max(1200, int(context_budget * 0.14)),
    )

    web_context = ""
    if web_mode == "on" or (web_mode == "auto" and should_use_web(message)):
        web_context = await gather_web_context(message, _noop_trace)

    prompt = build_research_prompt(
        message=message,
        mode=mode,
        document_context=doc_evidence.context,
        reference_context=ref_evidence.context,
        history_context=history_context,
        web_context=web_context,
    )

    from app.services.agentic_writer import AgenticWriter

    writer = AgenticWriter(llm)
    penalaran = "reasoning" in set(llm.capabilities or [])
    try:
        reply = await asyncio.wait_for(
            writer.ask(
                prompt,
                system=RESEARCH_CHAT_SYSTEM,
                temperature=0.15,
                max_tokens=output_token_budget(
                    # Cadangan penalaran diberikan tanpa memeriksa daftar
                    # kemampuan: model aktif proyek ini mendaftarkan diri sebagai
                    # ["text", "tools"] namun tetap mengirim jejak penalaran yang
                    # menghabiskan anggaran. Panjang jawaban sendiri sudah dibatasi
                    # oleh prompt ("Maksimal 220 kata"), jadi ruang tambahan ini
                    # tidak membuat jawaban jadi lebih panjang — hanya menghindari
                    # balasan kosong dan panggilan ulang yang menggandakan waktu.
                    message,
                    mode,
                    llm.context_window,
                    reasoning=True,
                ),
                images=images,
                reasoning_effort="low" if penalaran else None,
            ),
            # Batas yang sama dengan jalur AI lain (CO_WRITER_REQUEST_TIMEOUT_SECONDS):
            # sebelumnya chat dipatok 120 detik sendiri, jadi menaikkan batas lewat
            # env tidak berpengaruh di sini dan chat tetap 504 pada model penalaran
            # yang jalur /edit-nya sudah lolos.
            timeout=CO_WRITER_REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Model aktif tidak merespons dalam {CO_WRITER_REQUEST_TIMEOUT_SECONDS} "
                "detik. Konteks penelitian "
                "sudah disiapkan; coba lagi atau pilih model yang lebih cepat."
            ),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI gagal: {exc}")
    reply, invalid_citations = validate_citations(
        reply,
        set(ref_evidence.numbers),
    )
    if not reply:
        reply = (
            "Saya belum memperoleh jawaban yang dapat diverifikasi dari model aktif. "
            "Coba persempit pertanyaan ke satu bab, metode, atau klaim tertentu."
        )
    return {
        "reply": reply,
        "mode": mode,
        "model": {
            "name": llm.model_name,
            "profile": llm.profile_name,
        },
        "evidence": {
            "document_sections": doc_evidence.sections,
            "reference_numbers": ref_evidence.numbers,
            "web_used": bool(web_context),
            "invalid_citations_removed": invalid_citations,
        },
    }


@router.post("/documents/{doc_id}/regenerate-bibliography")
async def regenerate_bibliography(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate ulang Daftar Pustaka + rapatkan penomoran sitasi dokumen ini.

    Penomoran `[n]` dari `cite_add` bersifat global per-pengguna (append-only
    lintas dokumen), jadi laporan mandiri bisa mulai dari tengah (mis. `[13]`).
    Endpoint ini merapatkan penomoran KHUSUS dokumen: menulis ulang `[n]` di
    teks menjadi [1..N] menurut urutan kemunculan (gaya IEEE) DAN membangun
    Daftar Pustaka bernomor sama — keduanya dijaga sinkron, lalu disimpan
    sebagai isi dokumen (dengan checkpoint pemulihan).

    Body opsional: {"format": "ieee"} untuk memilih format (default ieee).
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    format_name = "ieee"
    if payload and payload.get("format"):
        format_name = str(payload["format"])

    from app.services.citation_formatter import (
        citation_meta_from_reference,
        generate_citation,
    )
    from app.services.citation_tools import (
        _peta_kemunculan,
        _tulis_ulang_nomor,
        compact_citations_and_bibliography,
    )

    # Ambil semua referensi user dalam urutan penomoran [n] — satu sumber
    # kebenaran bersama tool `cite_add` agen, supaya nomor yang dijanjikan ke
    # agen merujuk entri Daftar Pustaka yang sama.
    ordered = await referensi_urut(db, current_user.id)

    # Penomoran `[n]` dari `cite_add` bersifat global per-pengguna (append-only
    # lintas dokumen), jadi laporan mandiri bisa mulai dari tengah (mis. `[13]`).
    # Di sini penomoran DIRAPATKAN khusus dokumen ini: `[n]` di teks ditulis
    # ulang [1..N] menurut urutan kemunculan (gaya IEEE) dan Daftar Pustaka
    # dibangun seiras — keduanya dijaga sinkron.
    content = doc.content or ""

    if doc.content_format == "latex":
        # LaTeX: rapatkan [n] di badan (blok bibliografi lama dibuang saat
        # dihitung agar labelnya tak mengacaukan urutan), tulis ulang di seluruh
        # sumber, lalu bangun ulang blok bibliografi berurutan baru.
        badan = _LEGACY_LATEX_BIBLIOGRAPHY_RE.sub(
            "", _BIBLIOGRAPHY_BLOCK_RE.sub("", content)
        )
        peta = _peta_kemunculan(badan, len(ordered))
        content = _tulis_ulang_nomor(content, peta)
        entries = [
            f"[{baru}] {generate_citation(citation_meta_from_reference(ordered[lama - 1]), format_name)}"
            for lama, baru in sorted(peta.items(), key=lambda kv: kv[1])
        ]
        content, bib_section = _perbarui_bibliografi_latex(content, entries)
        used = sorted(peta.values())
    else:
        badan_baru, bib_md, used, asing, _peta = await compact_citations_and_bibliography(
            db, current_user.id, content, format_name=format_name
        )
        bib_section = bib_md or "## DAFTAR PUSTAKA\n\n_Belum ada sitasi di dokumen._"
        content = badan_baru.rstrip() + "\n\n" + bib_section

    if content != doc.content:
        await simpan_checkpoint(db, doc, current_user, "Sebelum regenerasi daftar pustaka")
        doc.content = content
    await db.commit()
    await db.refresh(doc)
    return {"bibliography": bib_section, "content": doc.content, "citation_count": len(used)}


@router.get("/citation-coverage")
async def citation_coverage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cakupan sitasi: referensi mana yang BENAR-BENAR dirujuk `[n]` di draf.

    Beda dari "sitasi tersimpan di pustaka" (`SavedCitation`): endpoint ini
    memindai penanda `[n]` pada naskah setiap draf lalu memetakannya ke referensi
    lewat `referensi_urut` — urutan penomoran yang sama dipakai `cite_add` agen
    dan `regenerate-bibliography`. Dipakai untuk analisis gap: sumber yang sudah
    dikumpulkan tapi belum sekali pun dipakai menulis.
    """
    ordered = await referensi_urut(db, current_user.id)
    docs = (
        await db.scalars(
            select(CoWriterDocument).where(CoWriterDocument.user_id == current_user.id)
        )
    ).all()

    cited_ids: set[str] = set()
    per_document: list[dict] = []
    for doc in docs:
        # Naskah didatarkan supaya sitasi di berkas bab (`\input`) ikut terhitung,
        # persis seperti regenerate-bibliography.
        naskah = await _sumber_tex_proyek(db, doc)
        used = {int(m) for m in re.findall(r"\[(\d+)\]", naskah)}
        used = {n for n in used if 1 <= n <= len(ordered)}
        if not used:
            continue
        for n in used:
            cited_ids.add(str(ordered[n - 1].id))
        per_document.append(
            {"doc_id": str(doc.id), "title": doc.title, "citation_count": len(used)}
        )

    total = len(ordered)
    return {
        "total_references": total,
        "cited_count": len(cited_ids),
        "uncited_count": max(0, total - len(cited_ids)),
        "cited_reference_ids": sorted(cited_ids),
        "per_document": per_document,
    }


@router.get("/documents/{doc_id}/export-latex")
async def export_latex(
    doc_id: uuid.UUID,
    format: str = "pdf",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export draf sebagai LaTeX: ?format=tex|pdf (PRD v2.5 Â§8).

    - format=tex â†’ unduh source .tex
    - format=pdf â†’ compile via tectonic â†’ unduh PDF rapi
    """
    import os
    import re as _re
    import tempfile

    doc = await _get_owned_doc(db, doc_id, current_user)

    safe_title = _re.sub(r'[\\/:*?"<>|]', "_", doc.title or "Draf").strip()[:80] or "Draf"
    fname = f"{safe_title.replace(' ', '_')}_{uuid.uuid4().hex[:6]}"

    # Markdown-first: dokumen utama menyimpan markdown, dan PDF-nya HARUS sama
    # dengan panel pratinjau. Jadi PDF dirender lewat jalur typeset (Chromium,
    # `markdown_to_typeset_html` yang sama dengan pratinjau) alih-alih
    # mengkompilasi markdown-sebagai-LaTeX lewat tectonic â€” kompilasi itulah
    # yang dulu membuat "yang diinput beda dengan yang keluar". Unduhan .tex
    # tetap dilayani (markdown->LaTeX) sebagai target ekspor opsional; draf lama
    # yang masih LaTeX murni tetap lewat tectonic di bawah.
    if doc.content_format == "markdown" and format == "pdf":
        from starlette.concurrency import run_in_threadpool

        from app.services.typeset import typeset_to_pdf

        tmpdir = tempfile.mkdtemp()
        output_path = os.path.join(tmpdir, f"{fname}.pdf")
        # Rapatkan penomoran sitasi [1..N] urut kemunculan untuk PDF ini
        # (penyimpanan tak diubah); tanpa ini dapus bisa mulai dari [13].
        from app.services.citation_tools import naskah_ekspor_rapat

        isi_pdf, _ = await naskah_ekspor_rapat(db, current_user.id, doc.content or "")
        try:
            await run_in_threadpool(typeset_to_pdf, isi_pdf, output_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Typeset PDF gagal untuk %s: %s", doc.id, exc, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Gagal membuat PDF: {exc}")
        return FileResponse(
            path=output_path,
            filename=f"{safe_title}.pdf",
            media_type="application/pdf",
        )

    # format=tex, atau draf lama yang masih LaTeX murni: butuh sumber .tex utuh.
    # `\input{}` sudah didatarkan agar naskahnya lengkap, bukan cuma preamble.
    tex_source = await _sumber_tex_proyek(db, doc)

    if format == "tex":
        # Unduh source .tex saja
        tmpdir = tempfile.mkdtemp()
        tex_path = os.path.join(tmpdir, f"{safe_title}.tex")
        with open(tex_path, "w", encoding="utf-8") as fh:
            fh.write(tex_source)
        return FileResponse(
            path=tex_path,
            filename=f"{safe_title}.tex",
            media_type="application/x-tex",
        )

    # format=pdf: tectonic atas sumber .tex yang tersimpan (jalur utama, sama
    # dengan /compile), lalu rantai cadangan bila kompilasi itu gagal ->
    # Pandoc -> DOCX (dengan notifikasi) -> HTML-to-PDF (Lapis 2, PRD v2.8 4.3).
    import unicodedata as _u

    from starlette.concurrency import run_in_threadpool

    from app.services import pandoc_latex

    tmpdir = tempfile.mkdtemp()
    images_dir = str(_document_upload_dir(doc.id) / "images")
    output_path = os.path.join(tmpdir, f"{fname}.pdf")

    # Jalur utama: kompilasi `tex_source` LANGSUNG -- sumber yang sama persis
    # dengan /compile dan panel pratinjau.
    #
    # Sebelumnya jalur ini memutar naskah lewat Markdown + Pandoc, dan itu
    # terukur merusak strukturnya: latex_to_markdown membuang bintang pada
    # \section* sehingga Pandoc menomori ulang seluruh judul, membuang
    # \newpage/\thispagestyle, dan melupakan lebar \includegraphics. Pada
    # laporan uji hasilnya 75 halaman sementara pratinjau dari sumber yang
    # sama 59 halaman -- inilah "yang diinputkan apa yang dikeluarkan apa"
    # yang dilaporkan pengguna. Pandoc tetap dipakai, tapi sebagai cadangan.
    try:
        pdf_langsung = await run_in_threadpool(
            compile_latex_pdf, tex_source, tmpdir, fname, [images_dir]
        )
        return FileResponse(
            path=pdf_langsung,
            filename=f"{safe_title}.pdf",
            media_type="application/pdf",
        )
    except Exception as exc_tex:  # noqa: BLE001
        logger.error(
            "Kompilasi LaTeX langsung gagal untuk %s: %s",
            doc.id, exc_tex, exc_info=True,
        )
        galat_tex = str(exc_tex).strip() or "kompilasi LaTeX gagal"
        # Nilai header HTTP harus ASCII (latin-1); log tectonic penuh em-dash.
        galat_tex_ascii = (
            _u.normalize("NFKD", galat_tex.splitlines()[-1][:160])
            .encode("ascii", "ignore")
            .decode()
        )

    if not pandoc_latex._tersedia():
        raise HTTPException(
            status_code=500,
            detail=(
                "Export PDF gagal: kompilasi LaTeX tidak berhasil dan Pandoc "
                f"tidak tersedia sebagai cadangan. Log: {galat_tex[-600:]}"
            ),
        )

    # Sumber markdown: prefer AST (Lapis 1) bila tersedia, agar rendering
    # konsisten dengan pratinjau; selain itu konversi LaTeX yang tersimpan.
    from app.services.doc_ast import DocumentAst, ast_to_markdown

    md = None
    if getattr(doc, "structured_content", None):
        try:
            md = ast_to_markdown(DocumentAst.from_json(doc.structured_content))
        except Exception:  # noqa: BLE001
            logger.warning("AST tidak bisa dibaca untuk %s â€” pakai LaTeX.", doc.id)
    if not md:
        md = latex_to_markdown(
            localize_latex_image_paths(
                tex_source,
                [str(_document_upload_dir(doc.id) / "images")],
            )
        )

    from starlette.concurrency import run_in_threadpool  # noqa: F811

    try:
        # Cadangan 1: Pandoc + template kampus -> tectonic.
        pdf_path = pandoc_latex.pandoc_to_pdf(
            md, output_path, asset_dirs=[images_dir], jobname=fname
        )
    except Exception as exc:  # noqa: BLE001
        # Â§4.3: log detail untuk debugging, tapi user hanya dapat pesan awam.
        logger.error(
            "Export LaTeX (pandoc/tectonic) gagal untuk %s: %s",
            doc.id, exc, exc_info=True,
        )
        alasan = str(exc).strip().splitlines()[-1][:160] if str(exc).strip() else "kompilasi gagal"
        alasan_ascii = _u.normalize("NFKD", alasan).encode("ascii", "ignore").decode()
        # Fallback 1: DOCX (jalur python-docx yang sudah matang).
        try:
            from app.models.journal import JournalReference
            from app.services.docx_template_exporter import (
                markdown_to_docx_template as markdown_to_docx,
            )

            refs = (
                await db.scalars(
                    select(JournalReference).where(
                        JournalReference.user_id == current_user.id
                    )
                )
            ).all()
            docx_path = os.path.join(tmpdir, f"{fname}.docx")
            await run_in_threadpool(
                markdown_to_docx, md, docx_path, references={
                    i: (r.doi if r.doi.startswith("http") else f"https://doi.org/{r.doi}")
                    for i, r in enumerate(
                        sorted(refs, key=lambda r: r.created_at), start=1
                    )
                    if r.doi
                },
            )
            return FileResponse(
                path=docx_path,
                filename=f"{safe_title}.docx",
                media_type=(
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                headers={
                    "X-Fallback-Notice": (
                        "Export PDF (LaTeX) gagal karena "
                        f"{alasan_ascii}; dibuatkan versi DOCX sebagai gantinya - "
                        "bisa dikonversi ke PDF lewat Word."
                    )
                },
            )
        except Exception as exc2:  # noqa: BLE001
            logger.error(
                "Fallback DOCX gagal untuk %s: %s", doc.id, exc2, exc_info=True
            )
        # Fallback 2: HTML-to-PDF dari Lapis 2 (Â§3) â€” render preview â†’ PDF.
        try:
            from app.services.typeset import typeset_to_pdf

            await run_in_threadpool(typeset_to_pdf, md, output_path)
        except Exception as exc3:  # noqa: BLE001
            logger.error(
                "Fallback HTML-to-PDF gagal untuk %s: %s", doc.id, exc3, exc_info=True
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    "Export PDF gagal di semua jalur (LaTeX, Word, pratinjau). "
                    "Coba periksa kembali isi dokumen, atau hubungi admin dengan "
                    f"pesan berikut: {alasan}"
                ),
            )
        return FileResponse(
            path=output_path,
            filename=f"{safe_title}.pdf",
            media_type="application/pdf",
            headers={
                "X-Fallback-Notice": (
                    "Export PDF (LaTeX) gagal karena "
                    f"{alasan_ascii}; PDF dibuat dari pratinjau layar sebagai gantinya."
                )
            },
        )
    # Pandoc berhasil, tapi ini tetap CADANGAN: naskahnya sudah lewat Markdown,
    # jadi penomoran judul dan jeda halaman bisa berbeda dari pratinjau.
    # Pengguna perlu diberi tahu, kalau tidak selisihnya tampak seperti cacat.
    return FileResponse(
        path=pdf_path,
        filename=f"{safe_title}.pdf",
        media_type="application/pdf",
        headers={
            "X-Fallback-Notice": (
                "Kompilasi LaTeX langsung gagal karena "
                f"{galat_tex_ascii}; PDF dibuat lewat jalur Pandoc, jadi "
                "penomoran judul dan jeda halaman bisa berbeda dari pratinjau."
            )
        },
    )


@router.post("/documents/{doc_id}/compile")
async def compile_preview(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Kompilasi kode LaTeX di editor jadi PDF untuk panel pratinjau.

    Body opsional: {"content": "<kode tex>", "path": "bab/01-x.tex"}. Bila
    diberikan, `content` adalah isi editor yang BELUM disimpan supaya pratinjau
    mengikuti ketikan; `path` menyebutkan berkas mana yang sedang dibuka.

    Yang dikompilasi selalu PROYEK UTUH, bukan berkas yang sedang dibuka: satu
    bab tanpa preamble bukan dokumen LaTeX yang sah, jadi mengkompilasinya
    sendirian hanya menghasilkan galat. Isi editor disuntikkan ke posisi
    berkasnya, lalu `\\input{}` didatarkan seperti biasa.

    Galat sintaks LaTeX dibalas 422 (bukan 500): saat mengetik, dokumen yang
    belum lengkap adalah keadaan normal, dan frontend perlu menampilkan lognya.
    """
    import os
    import tempfile

    from fastapi.concurrency import run_in_threadpool

    doc = await _get_owned_doc(db, doc_id, current_user)
    data = payload or {}
    isi_diberikan = "content" in data
    isi_editor = data.get("content")
    jalur = data.get("path")
    if "path" in data and jalur is not None and not isinstance(jalur, str):
        raise HTTPException(status_code=422, detail="path harus berupa teks.")
    berkas = await _muat_berkas(db, doc.id)
    try:
        tex_source = _sumber_preview_proyek(
            _sumber_tex(doc),
            berkas,
            isi_editor=isi_editor if isinstance(isi_editor, str) else None,
            jalur_editor=jalur if isinstance(jalur, str) and jalur.strip() else None,
            isi_diberikan=isi_diberikan,
        )
    except (JalurTidakSah, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    tmpdir = tempfile.mkdtemp()
    try:
        pdf_path = await run_in_threadpool(
            compile_latex_pdf,
            tex_source,
            tmpdir,
            "pratinjau",
            [str(_document_upload_dir(doc.id) / "images")],
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Kompilasi LaTeX gagal.", "log": str(exc)[-4000:]},
        )
    return FileResponse(
        path=pdf_path,
        filename="pratinjau.pdf",
        media_type="application/pdf",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/documents/{doc_id}/checkpoints")
async def list_checkpoints(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daftar checkpoint dokumen (PRD v2.4 Â§6) â€” terbaru di atas."""
    from app.models.co_writer_checkpoint import CoWriterCheckpoint

    await _get_owned_doc(db, doc_id, current_user)
    cps = (
        await db.scalars(
            select(CoWriterCheckpoint)
            .where(
                CoWriterCheckpoint.doc_id == doc_id,
                CoWriterCheckpoint.user_id == current_user.id,
            )
            .order_by(CoWriterCheckpoint.created_at.desc())
            .limit(20)
        )
    ).all()
    return {
        "checkpoints": [
            {
                "id": str(cp.id),
                "label": cp.label,
                "created_at": int(cp.created_at.timestamp()),
                "content_length": len(_urai_snapshot_proyek(cp.content_snapshot)[0]),
                "file_count": len(_urai_snapshot_proyek(cp.content_snapshot)[1] or {}),
            }
            for cp in cps
        ]
    }


_CHECKPOINT_PROJECT_PREFIX = "nalar-project-v1\n"


def _urai_snapshot_proyek(snapshot: str) -> tuple[str, dict[str, str] | None]:
    """Baca checkpoint proyek baru; checkpoint lama tetap dianggap main.tex."""
    if not (snapshot or "").startswith(_CHECKPOINT_PROJECT_PREFIX):
        return snapshot or "", None
    try:
        payload = json.loads(snapshot[len(_CHECKPOINT_PROJECT_PREFIX) :])
        main = payload.get("main", "")
        files = payload.get("files", {})
        if not isinstance(main, str) or not isinstance(files, dict):
            raise ValueError("format snapshot tidak sah")
        normalized = {
            str(path): str(content)
            for path, content in files.items()
            if isinstance(path, str) and isinstance(content, str)
        }
        return main, normalized
    except (ValueError, TypeError, json.JSONDecodeError):
        # Jangan membuat checkpoint rusak tidak dapat dipulihkan sama sekali.
        return snapshot or "", None


async def simpan_checkpoint(
    db: AsyncSession,
    doc: CoWriterDocument,
    user: User,
    label: str,
) -> "CoWriterCheckpoint":  # noqa: F821
    """Simpan snapshot isi dokumen saat ini. Belum commit â€” pemanggil yang commit.

    Dipakai rute POST /checkpoints dan konversi otomatis ke LaTeX di
    get_document, supaya logikanya tidak diduplikasi.
    """
    from app.models.co_writer_checkpoint import CoWriterCheckpoint

    files = await _muat_berkas(db, doc.id)
    snapshot = _CHECKPOINT_PROJECT_PREFIX + json.dumps(
        {"main": doc.content or "", "files": files},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cp = CoWriterCheckpoint(
        doc_id=doc.id,
        user_id=user.id,
        label=(label.strip() or "Checkpoint")[:255],
        content_snapshot=snapshot,
    )
    db.add(cp)
    return cp


@router.post("/documents/{doc_id}/checkpoints")
async def create_checkpoint(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Simpan snapshot isi dokumen SAAT INI sebagai checkpoint.

    Body opsional: {"label": "Sebelum: tulis Bab 2"}.
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    label = str((payload or {}).get("label", "")).strip()
    cp = await simpan_checkpoint(db, doc, current_user, label)
    await db.commit()
    await db.refresh(cp)
    return {
        "id": str(cp.id),
        "label": cp.label,
        "created_at": int(cp.created_at.timestamp()),
    }


@router.get("/documents/{doc_id}/checkpoints/{cp_id}")
async def get_checkpoint(
    doc_id: uuid.UUID,
    cp_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ambil isi satu checkpoint (untuk diff)."""
    from app.models.co_writer_checkpoint import CoWriterCheckpoint

    await _get_owned_doc(db, doc_id, current_user)
    cp = await db.scalar(
        select(CoWriterCheckpoint).where(
            CoWriterCheckpoint.id == cp_id,
            CoWriterCheckpoint.doc_id == doc_id,
            CoWriterCheckpoint.user_id == current_user.id,
        )
    )
    if cp is None:
        raise HTTPException(status_code=404, detail="Checkpoint tidak ditemukan.")
    content, files = _urai_snapshot_proyek(cp.content_snapshot)
    return {
        "content": content,
        "files": sorted((files or {}).keys()),
        "label": cp.label,
    }


@router.post("/documents/{doc_id}/checkpoints/{cp_id}/restore")
async def restore_checkpoint(
    doc_id: uuid.UUID,
    cp_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Kembalikan isi dokumen ke versi checkpoint (PRD v2.4 Â§6)."""
    from app.models.co_writer_checkpoint import CoWriterCheckpoint

    doc = await _get_owned_doc(db, doc_id, current_user)
    cp = await db.scalar(
        select(CoWriterCheckpoint).where(
            CoWriterCheckpoint.id == cp_id,
            CoWriterCheckpoint.doc_id == doc_id,
            CoWriterCheckpoint.user_id == current_user.id,
        )
    )
    if cp is None:
        raise HTTPException(status_code=404, detail="Checkpoint tidak ditemukan.")
    # Snapshot kondisi saat ini sebelum timpa (anti-hilang).
    await simpan_checkpoint(db, doc, current_user, f"Sebelum restore: {cp.label}")
    restored_main, restored_files = _urai_snapshot_proyek(cp.content_snapshot)
    doc.content = restored_main
    if restored_files is not None:
        current_rows = (
            await db.scalars(select(CoWriterFile).where(CoWriterFile.doc_id == doc.id))
        ).all()
        current_by_path = {row.path: row for row in current_rows}
        for path, content in restored_files.items():
            row = current_by_path.pop(path, None)
            if row is None:
                db.add(
                    CoWriterFile(
                        doc_id=doc.id,
                        user_id=current_user.id,
                        path=path,
                        content=content,
                    )
                )
            else:
                row.content = content
        for obsolete in current_by_path.values():
            await db.delete(obsolete)
    await db.commit()
    await db.refresh(doc)
    return _detail(doc)


@router.get("/documents/{doc_id}/typeset")
async def typeset_preview(    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pratinjau Rapi (PRD v2.8 Â§3): render dari AST bila tersedia, tanpa
    memanggil kompiler LaTeX. Dokumen lama tanpa AST jatuh ke render markdown
    yang sudah ada (PRV01: preview tidak pernah bergantung pada compile)."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    if doc.structured_content:
        try:
            from app.services.doc_ast import DocumentAst, ast_to_html

            ast = DocumentAst.from_json(doc.structured_content)
            return {"html": ast_to_html(ast)}
        except Exception:  # noqa: BLE001 â€” AST rusak â†’ fallback render lama
            pass
    from app.services.typeset import markdown_to_typeset_html

    return {"html": markdown_to_typeset_html(await _sumber_tex_proyek(db, doc))}


@router.post("/documents/{doc_id}/typeset")
async def typeset_preview_buffer(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pratinjau Rapi dari isi editor yang BELUM disimpan (PRV03).

    Body opsional: {"content": "<markdown>"}. Dipakai panel pratinjau saat
    mengetik supaya tidak perlu menyimpan tiap ketikan.

    Markdown-first: pratinjau memakai renderer YANG SAMA dengan ekspor PDF
    (`markdown_to_typeset_html` â€” markdown2), jadi yang di layar == yang
    tercetak. Ringan, dan tidak bisa gagal seperti compile LaTeX.
    """
    await _get_owned_doc(db, doc_id, current_user)
    content = (payload or {}).get("content")
    if content is not None and isinstance(content, str):
        from app.services.typeset import markdown_to_typeset_html

        return {"html": markdown_to_typeset_html(content), "source": "buffer"}
    from app.services.typeset import markdown_to_typeset_html

    doc = await _get_owned_doc(db, doc_id, current_user)
    # Dokumen markdown: render kolom content langsung (== ekspor). Dokumen lama
    # yang belum dimigrasi (masih LaTeX) dirapikan lewat sumber proyek.
    if doc.content_format == "markdown":
        return {"html": markdown_to_typeset_html(doc.content or ""), "source": "doc"}
    return {
        "html": markdown_to_typeset_html(await _sumber_tex_proyek(db, doc)),
        "source": "fallback",
    }


@router.post("/documents/{doc_id}/ai-checks")
async def ai_checks(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Scan dokumen (PRD v2.4 Â§3,5):

    - claims_without_citation: kalimat ber-pola klaim/fakta tanpa sitasi [n].
    - terminology: variasi penulisan istilah yang sama (konsistensi).
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    content = await _sumber_tex_proyek(db, doc)

    # â”€â”€ Deteksi klaim tanpa sitasi (heuristik lokal, tanpa LLM) â”€â”€
    claims: list[str] = []
    claim_pattern = re.compile(
        r"(menurut|berdasarkan|hasil (penelitian|studi)|penelitian (menunjukkan|menemukan)|"
        r"ditemukan|mencapai|sebesar|meningkat|menurun|terbukti|efektif|akurat|"
        r"berdasarkan data|statistik|persen|%)\b",
        re.I,
    )
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Punya sitasi? ([n] atau (Penulis, tahun))
        has_citation = bool(
            re.search(r"\[\d+\]|\(\s*[A-Z][A-Za-z]+[^)]*\d{4}\s*\)", line)
        )
        if claim_pattern.search(line) and not has_citation:
            claims.append(line[:300])

    # â”€â”€ Cek konsistensi istilah (heuristik) â”€â”€
    terms: dict[str, dict] = {}
    term_patterns = [
        (r"\bNalar\s*AI\b", "Nalar AI"),
        (r"\bNalarAI\b", "NalarAI"),
        (r"\bNalar\.ai\b", "Nalar.ai"),
        (r"\bRAG\b", "RAG"),
        (r"\bRetrieval[- ]Augmented[- ]Generation\b", "Retrieval-Augmented Generation"),
        (r"\bLLM\b", "LLM"),
        (r"\bLarge Language Model[s]?\b", "Large Language Model"),
    ]
    for pattern, label in term_patterns:
        count = len(re.findall(pattern, content, re.I))
        if count > 0:
            terms[label] = {"count": count, "variants": [label]}

    # Agregasi varian: Nalar AI vs NalarAI vs Nalar.ai
    nalar_variants = [k for k in terms if "nalar" in k.lower()]
    if len(nalar_variants) > 1:
        total = sum(terms[k]["count"] for k in nalar_variants)
        terms["Nalar AI (seragamkan)"] = {
            "count": total,
            "variants": nalar_variants,
            "suggest": "Nalar AI",
        }

    suggestions = []
    if (payload or {}).get("suggest_references") and claims:
        # Cari hanya kandidat teratas agar pemeriksaan tetap cepat. Pengguna
        # harus memilih kandidat sebelum metadata masuk ke grup referensi.
        suggestions = await search_academic_references(claims[0], limit=3)
    return {
        "claims_without_citation": claims[:20],
        "claim_count": len(claims),
        "reference_suggestions": suggestions,
        "terminology": [
            {
                "term": k,
                "count": v["count"],
                "variants": v.get("variants", [k]),
                "suggest": v.get("suggest"),
            }
            for k, v in terms.items()
        ],
    }


# ── Stage 3: LLM targeted review (claude-like-reviewer) ────────────────
# Membungkus hasil pre-filter deterministik (ai-checks + gap-analysis) menjadi
# feedback ala editor manusia: lokasi presisi, level, suggested_action,
# tool_to_call. Model-agnostic — pipeline yang bikin teliti, bukan model.


def _extract_json_array(raw: str) -> list | None:
    """Ekstrak JSON array dari respons LLM (tahan bungkus ```json ... ```).

    Return list bila ketemu & valid, None bila tidak ada JSON array yang bisa
    di-parse. Dipakai juga untuk fallback reasoning_content.

    CATATAN: regex memakai pencocokan KURUNG SEIMBANG (bukan non-greedy)
    supaya `[3]` literal di teks (mis. nomor kandidat "[3] (Bab 4.1)") tidak
    tertangkap sebagai array JSON.
    """
    if not raw or not raw.strip():
        return None
    # cari posisi '[' pertama, lalu coba parse dari sana dengan bracket matching
    for start in [m.start() for m in re.finditer(r"\[", raw)]:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    candidate = raw[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except (TypeError, ValueError):
                        break  # bukan JSON valid — coba posisi '[' berikutnya
                    # Array temuan reviewer = array objek dict. Array berisi
                    # angka (`[3]` dari nomor kandidat literal) BUKAN temuan.
                    if isinstance(parsed, list) and parsed and all(
                        isinstance(x, dict) for x in parsed
                    ):
                        return parsed
                    if isinstance(parsed, list) and not parsed:
                        return parsed  # [] = LLM menolak semua — valid
    return None

_REVIEWER_SYSTEM_PROMPT = """Kamu adalah editor naskah ilmiah berpengalaman yang sedang membaca laporan mahasiswa/peneliti untuk memberi catatan pinggir, bukan AI checker atau QA bot. Kamu HANYA menilai satu titik dalam dokumen pada satu waktu, dengan konteks yang diberikan di bawah - jangan menyimpulkan hal yang tidak ada di konteks itu.

KONTEKS GLOBAL DOKUMEN (ringkasan tiap bab, untuk kamu tahu apa yang sudah/belum dibahas di bab lain):
{{chapter_summaries}}

BAGIAN YANG SEDANG DINILAI:
Bab: {{chapter_number}} - {{chapter_title}}
Paragraf ke-{{paragraph_number}} (beserta 1 paragraf sebelum dan sesudahnya sebagai konteks lokal):
{{local_context_with_target_paragraph_marked}}

KANDIDAT MASALAH DARI PRE-FILTER (hasil deteksi pola otomatis, kamu validasi dan detailkan, bukan mulai dari nol):
{{prefilter_candidate_description}}

TUGASMU:
1. Konfirmasi apakah kandidat ini benar masalah nyata (bukan false positive dari pattern matching). Kalau ternyata bukan masalah, keluarkan array kosong.
2. Kalau benar masalah, tentukan level-nya:
   - minor: typo, spasi ganda, inkonsistensi istilah kecil, formatting.
   - moderate: kalimat ambigu, alur paragraf kurang rapi, referensi silang antar bab kurang jelas.
   - serious: klaim tanpa data/sitasi pendukung, bagian standar artikel ilmiah yang hilang, kontradiksi antar bagian dokumen.
3. Tulis 'issue' dalam SATU kalimat pendek, spesifik ke kalimat/kata yang bermasalah - bukan komentar umum soal keseluruhan bagian.
4. Tulis 'suggested_action' sebagai instruksi konkret yang bisa langsung dieksekusi lewat tool, bukan saran naratif panjang. Kalau butuh keputusan user, tulis sebagai pertanyaan singkat dengan maksimal 2 pilihan.
5. Petakan ke SATU tool yang paling sesuai: insert_citation, insert_or_edit_section, fix_minor_issue, atau null.
6. Sertakan 'anchor_text': cuplikan LITERAL maksimal 1 kalimat dari paragraf yang dinilai, untuk scroll-to-location. Jangan parafrase.

GAYA BAHASA:
- Singkat dan langsung ke poin, seperti catatan pinggir editor manusia, bukan laporan bug.
- Sopan tapi tidak berbasa-basi. Tidak ada kalimat pembuka seperti 'Berikut adalah beberapa saran untuk meningkatkan kualitas dokumen Anda:'.
- Bahasa Indonesia baku tapi natural, seperti editor jurnal menulis catatan ke penulis.

FORMAT OUTPUT (WAJIB JSON valid, array dari 0 atau lebih temuan, tidak ada teks lain di luar JSON):
[
  {{
    "location": {{"chapter": "{{chapter_number}}", "paragraph": {paragraph_number}, "anchor_text": "..."}},
    "level": "minor | moderate | serious",
    "issue": "...",
    "suggested_action": "...",
    "tool_to_call": "insert_citation | insert_or_edit_section | fix_minor_issue | null"
  }}
]"""


_REVIEW_HEADING_RE = re.compile(
    r"^\\(?:chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}"
)


def _review_heading_label(ln: str) -> str | None:
    """Kalau `ln` sebuah heading (LaTeX atau Markdown), kembalikan judulnya.

    Dipakai reviewer untuk melacak bab/section yang sedang berjalan supaya lokasi
    temuan (chapter + paragraf) dihitung DETERMINISTIK dari struktur dokumen —
    bukan ditebak LLM, yang cenderung menyalin contoh format (dulu selalu "Bab 1").
    """
    m = _REVIEW_HEADING_RE.match(ln)
    if m:
        return m.group(1).strip() or None
    if ln.startswith("#"):
        return ln.lstrip("#").strip() or None
    return None


def _lokasi_temuan(finding: dict, candidates: list[dict]) -> dict:
    """Ganti lokasi tebakan LLM dengan lokasi nyata dari kandidat pre-filter.

    Kita hanya percaya `candidate_index` + `anchor_text` dari LLM; chapter &
    paragraph diambil dari kandidat yang posisinya sudah dilacak saat pre-filter.
    """
    idx = finding.get("candidate_index")
    cand = candidates[idx - 1] if isinstance(idx, int) and 1 <= idx <= len(candidates) else None
    loc = finding.get("location") if isinstance(finding.get("location"), dict) else {}
    anchor = str(loc.get("anchor_text") or finding.get("anchor_text") or "").strip()
    if cand:
        finding["location"] = {
            "chapter": cand["chapter"],
            "paragraph": cand["paragraph"],
            "anchor_text": anchor or cand["text"][:120],
        }
    else:
        # candidate_index tak valid — pertahankan anchor, jangan mengarang bab.
        finding["location"] = {
            "chapter": loc.get("chapter") or "?",
            "paragraph": loc.get("paragraph"),
            "anchor_text": anchor,
        }
    finding.pop("candidate_index", None)
    return finding


@router.post("/documents/{doc_id}/ai-review")
async def ai_review(
    doc_id: uuid.UUID,
    payload: dict | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stage 3: LLM targeted review ala editor manusia.

    Memakai hasil pre-filter deterministik (klaim tanpa sitasi, gap struktur)
    sebagai kandidat, lalu LLM menvalidasi & merinci jadi temuan actionable
    dengan lokasi presisi — tanpa mengirim seluruh dokumen ke LLM.
    """
    from openai import AsyncOpenAI

    doc = await _get_owned_doc(db, doc_id, current_user)
    content = await _sumber_tex_proyek(db, doc)
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]

    # ── Konteks global ringkas (chapter_summaries) — stage 2 ──
    chapters: list[str] = []
    current_chapter = "Umum"
    chapter_lines: list[str] = []
    for ln in lines:
        if ln.startswith(("\\chapter", "\\section", "#")):
            if chapter_lines:
                chapters.append(f"{current_chapter}: {' '.join(chapter_lines)[:200]}")
            current_chapter = ln.replace("\\chapter{", "").replace("\\section{", "").replace("#", "").strip("{} ")
            chapter_lines = []
        else:
            chapter_lines.append(ln)
    if chapter_lines:
        chapters.append(f"{current_chapter}: {' '.join(chapter_lines)[:200]}")
    chapter_summaries = "\n".join(chapters[:30]) or "(dokumen pendek)"

    # ── Kandidat dari pre-filter (stage 1) — klaim tanpa sitasi ──
    data = payload or {}
    max_items = int(data.get("max_items") or 5)
    candidates: list[str] = []
    claim_pattern = re.compile(
        r"(menurut|berdasarkan|hasil (penelitian|studi)|penelitian (menunjukkan|menemukan)|"
        r"ditemukan|mencapai|sebesar|meningkat|menurun|terbukti|efektif|akurat|"
        r"berdasarkan data|statistik|persen|%)\b",
        re.I,
    )
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        has_citation = bool(re.search(r"\[\d+\]|\(\s*[A-Z][A-Za-z]+[^)]*\d{4}\s*\)", ln))
        if claim_pattern.search(ln) and not has_citation:
            candidates.append(ln[:300])
        if len(candidates) >= max_items:
            break

    # ── Resolve LLM (model aktif user) ──
    llm = await _resolve_llm(db, current_user.id)
    client = AsyncOpenAI(base_url=llm.base_url, api_key=llm.api_key)

    findings: list[dict] = []
    failed_candidates = 0
    # Stage 3+4: BATCHED targeted review — semua kandidat dalam SATU panggilan
    # LLM (model murah seperti kimi bisa 60-100s per call; per-kandidat berarti
    # N×lambat). Kandidat minor digabung; tetap dengan konteks lokal ringkas.
    if candidates:
        cand_block = "\n\n".join(
            f"[{i + 1}] (Bab 1, paragraf {lines.index(c) + 1 if c in lines else i + 1}): {c[:250]}"
            for i, c in enumerate(candidates)
        )
        batch_prompt = (
            "Kamu adalah editor naskah ilmiah berpengalaman memberi catatan pinggir, "
            "bukan AI checker. Konteks global dokumen:\n"
            f"{chapter_summaries}\n\n"
            "KANDIDAT MASALAH DARI PRE-FILTER (validasi masing-masing, jangan mulai dari nol):\n"
            f"{cand_block}\n\n"
            "TUGAS: Untuk SETIAP kandidat, konfirmasi apakah masalah nyata. Kalau bukan, "
            "LEWATKAN (jangan masukkan ke output). Untuk yang benar, tentukan level "
            "(minor/moderate/serious), tulis issue SATU kalimat spesifik, suggested_action "
            "konkret yang bisa dieksekusi tool, tool_to_call (insert_citation | "
            "insert_or_edit_section | fix_minor_issue | null), dan anchor_text LITERAL "
            "maks 1 kalimat dari kandidat. Nada: catatan pinggir editor manusia, singkat, "
            "Bahasa Indonesia natural. JANGAN ada kalimat pembuka template.\n\n"
            "PEDOMAN LEVEL (ikuti PERSIS):\n"
            "- serious HANYA untuk klaim yang SAMA SEKALI TIDAK punya sitasi/data pendukung, "
            "atau kontradiksi, atau bagian standar hilang.\n"
            "- Kalau klaim PUNYA sitasi tapi sitasinya berpotensi kurang relevan/ketinggalan zaman "
            "untuk topik yang berkembang cepat (AI, LLM, teknologi terkini) — itu level moderate, "
            "BUKAN serious, karena penulis SUDAH berusaha memberi dukungan, cuma kualitasnya perlu "
            "ditingkatkan. JANGAN samakan 'sitasi kurang kuat' dengan 'tidak ada sitasi'.\n"
            "- Kalimat ambigu/janggal tanpa masalah substansi ilmiah = minor atau moderate, bukan serious.\n"
            "- Kalau kandidat MENYERTAKAN bukti (mis. 'Berdasarkan Tabel 4.2', angka hasil pengujian, "
            "atau sitasi eksplisit), itu BUKAN klaim tanpa dukungan — LEWATKAN (0 temuan) kecuali ada "
            "masalah lain yang jelas.\n"
            "- SETIAP temuan WAJIB punya field level valid (minor/moderate/serious). Jangan keluarkan "
            "temuan tanpa level.\n\n"
            "CONTOH: 'Menurut Smith (2010), fine-tuning LLM terbukti efektif' → level moderate, "
            "suggested_action: 'Sitasi ini dari 2010, sementara topik LLM berkembang pesat — "
            "pertimbangkan tambah sitasi yang lebih baru sebagai pendukung tambahan.'\n\n"
            'FORMAT OUTPUT: JSON array saja (0 atau lebih objek), tidak ada teks lain:\n'
            '[{"location": {"chapter": "1", "paragraph": N, "anchor_text": "..."}, '
            '"level": "minor|moderate|serious", "issue": "...", "suggested_action": "...", '
            '"tool_to_call": "..."}]'
        )
        try:
            resp = await client.chat.completions.create(
                model=llm.model_name,
                messages=[
                    {"role": "system", "content": batch_prompt},
                    {"role": "user", "content": "Validasi semua kandidat dan keluarkan JSON array."},
                ],
                temperature=0.2,
                max_tokens=3000,
            )
            msg = resp.choices[0].message
            raw = (msg.content or "").strip()
            # Jaring pengaman: model reasoning (mis. deepseek-v4-flash-free)
            # kadang menghabiskan token di reasoning_content sebelum sempat
            # menulis content. Kalau content kosong/tidak berisi JSON, coba
            # ekstrak dari reasoning_content sebelum menganggap gagal.
            used_reasoning_fallback = False
            if not _extract_json_array(raw):
                reasoning = getattr(msg, "reasoning_content", None) or ""
                if reasoning.strip():
                    raw = reasoning
                    used_reasoning_fallback = True
                    logging.getLogger("nalar.ai_review").warning(
                        "ai_review fallback reasoning_content terpakai (content kosong/tanpa JSON) "
                        "doc=%s model=%s",
                        doc_id, llm.model_name,
                    )
            parsed = _extract_json_array(raw)
            if parsed:
                findings.extend(parsed)
            elif used_reasoning_fallback or not (msg.content or "").strip():
                # KEDUANYA kosong/tanpa JSON — log jelas, tandai kandidat gagal
                # (bukan crash / silent failure). UI menampilkan pesan partial.
                failed_candidates = len(candidates)
                logging.getLogger("nalar.ai_review").warning(
                    "ai_review: LLM response tidak berisi JSON valid di content maupun "
                    "reasoning_content (doc=%s model=%s) — %d kandidat gagal diproses",
                    doc_id, llm.model_name, failed_candidates,
                )
        except Exception as exc:  # noqa: BLE001 — LLM gagal, fallback ke pre-filter
            failed_candidates = len(candidates)
            findings.extend(
                {
                    "location": {"chapter": "1", "paragraph": i + 1, "anchor_text": c[:80]},
                    "level": "moderate",
                    "issue": f"(gagal divalidasi LLM: {exc}) Kandidat dilaporkan dari pre-filter.",
                    "suggested_action": "Periksa kalimat ini manual.",
                    "tool_to_call": None,
                }
                for i, c in enumerate(candidates)
            )

    # Kelompokkan: serious dulu, lalu moderate, minor
    order = {"serious": 0, "moderate": 1, "minor": 2}
    findings.sort(key=lambda f: order.get(f.get("level", "minor"), 3))
    return {
        "findings": findings,
        "candidates_reviewed": len(candidates),
        "total_findings": len(findings),
        "meta": {
            "total_candidates": len(candidates),
            "failed_candidates": failed_candidates,
            "partial": failed_candidates > 0,
        },
    }


@router.post("/documents/{doc_id}/reference-suggestions")
async def reference_suggestions(
    doc_id: uuid.UUID,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cari sumber asli untuk satu klaim dan verifikasi DOI sebelum ditawarkan."""
    await _get_owned_doc(db, doc_id, current_user)
    claim = str((payload or {}).get("claim") or "").strip()
    if len(claim) < 8:
        raise HTTPException(status_code=422, detail="Klaim terlalu pendek untuk pencarian akademik.")
    return {"claim": claim, "candidates": await search_academic_references(claim, int((payload or {}).get("limit") or 5))}


@router.post("/documents/{doc_id}/replace-term")
async def replace_document_term(
    doc_id: uuid.UUID,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Seragamkan satu variasi istilah di seluruh proyek dengan checkpoint."""
    source_term = str(payload.get("from", "")).strip()
    target_term = str(payload.get("to", "")).strip()
    if not source_term or not target_term:
        raise HTTPException(status_code=422, detail="Istilah asal dan tujuan wajib diisi.")
    if len(source_term) > 120 or len(target_term) > 120:
        raise HTTPException(status_code=422, detail="Istilah terlalu panjang.")
    if source_term.casefold() == target_term.casefold():
        return {"replaced": 0, "files_changed": 0}

    doc = await _get_owned_doc(db, doc_id, current_user)
    await simpan_checkpoint(
        db,
        doc,
        current_user,
        f"Sebelum seragamkan: {source_term} -> {target_term}",
    )
    pattern = re.compile(re.escape(source_term), flags=re.IGNORECASE)
    doc.content, replaced = pattern.subn(lambda _: target_term, doc.content or "")
    files_changed = 0
    rows = (
        await db.scalars(select(CoWriterFile).where(CoWriterFile.doc_id == doc.id))
    ).all()
    for row in rows:
        updated, count = pattern.subn(lambda _: target_term, row.content or "")
        if count:
            row.content = updated
            replaced += count
            files_changed += 1
    await db.commit()
    return {"replaced": replaced, "files_changed": files_changed}


@router.get("/documents/{doc_id}/gap-analysis")
async def gap_analysis(
    doc_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bandingkan heading dokumen vs struktur standar (PRD v2.4 Â§2)."""
    from app.services.typeset import markdown_to_typeset_html

    doc = await _get_owned_doc(db, doc_id, current_user)
    STANDARD = [
        "Abstrak",
        "Pendahuluan",
        "Tinjauan Pustaka",
        "Metodologi",
        "Hasil",
        "Pembahasan",
        "Kesimpulan",
        "Daftar Pustaka",
    ]
    headings = [
        judul.lower()
        for ln in (await _sumber_tex_proyek(db, doc)).splitlines()
        if (judul := judul_dari_latex(ln))
    ]
    results = []
    for section in STANDARD:
        found = any(section.lower() in h for h in headings)
        results.append(
            {
                "section": section,
                "present": found,
                "status": "ada" if found else "belum ada",
            }
        )
    return {"sections": results, "total_present": sum(1 for r in results if r["present"])}


@router.get("/documents/{doc_id}/export")
async def export_document(
    doc_id: uuid.UUID,
    format: str = "docx",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export draf: ?format=pdf|docx (default docx). Menjaga heading/numbering/tabel."""
    import os
    import re as _re
    import tempfile

    # Memakai template resmi kampus agar margin, font, dan spasi sesuai
    # aturan; jatuh ke exporter biasa bila templatenya tidak ditemukan.
    from app.services.docx_template_exporter import (
        markdown_to_docx_template as markdown_to_docx,
    )
    from app.services.typeset import typeset_to_pdf

    doc = await _get_owned_doc(db, doc_id, current_user)

    # DOI mapping untuk sitasi aktif di DOCX — urutan [n] dari sumber kebenaran
    # yang sama dengan tool `cite_add` agen.
    ordered = await referensi_urut(db, current_user.id)
    references: dict[int, str] = {}
    for i, ref in enumerate(ordered, start=1):
        doi = (ref.doi or "").strip()
        if doi:
            references[i] = doi if doi.startswith("http") else f"https://doi.org/{doi}"

    # Jembatan: draf disimpan sebagai LaTeX murni, tapi pengekspor PDF/DOCX
    # yang lama membaca Markdown. Konversi on-the-fly tanpa mengubah penyimpanan.
    from app.services.latex_export import latex_to_markdown
    isi = doc.content or ""
    if doc.content_format == "latex":
        tex_source = await _sumber_tex_proyek(db, doc)
        isi = latex_to_markdown(
            localize_latex_image_paths(
                tex_source,
                [str(_document_upload_dir(doc.id) / "images")],
            )
        )

    # Rapatkan penomoran sitasi [1..N] menurut urutan kemunculan KHUSUS untuk
    # keluaran ini — penyimpanan tak diubah, tapi PDF/DOCX tak lagi mulai dari
    # tengah (mis. [13]) hanya karena perpustakaan memuat referensi laporan
    # lain. `references` (DOI) berkunci nomor global, jadi ikut dipetakan ulang.
    from app.services.citation_tools import naskah_ekspor_rapat

    isi, _peta_sitasi = await naskah_ekspor_rapat(db, current_user.id, isi)
    if _peta_sitasi:
        references = {
            _peta_sitasi[lama]: doi
            for lama, doi in references.items()
            if lama in _peta_sitasi
        }

    safe_title = _re.sub(r'[\\/:*?"<>|]', "_", doc.title or "Draf").strip()[:80] or "Draf"
    fname = f"{safe_title.replace(' ', '_')}_{uuid.uuid4().hex[:6]}"

    # Render PDF/DOCX berat dan sepenuhnya sinkron; dijalankan di threadpool
    # agar event loop tetap bisa melayani permintaan lain selama ekspor.
    from starlette.concurrency import run_in_threadpool

    if format == "pdf":
        output_path = os.path.join(tempfile.gettempdir(), f"{fname}.pdf")
        try:
            # Lewat jalur typeset: heading/gambar/tabel bernomor, daftar isi
            # rapi, dan dirender Chromium sehingga sama dengan pratinjau layar.
            await run_in_threadpool(typeset_to_pdf, isi, output_path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Gagal membuat PDF: {exc}")
        media = "application/pdf"
        ext = ".pdf"
    else:
        output_path = os.path.join(tempfile.gettempdir(), f"{fname}.docx")
        try:
            await run_in_threadpool(
                markdown_to_docx, isi, output_path, references
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Gagal membuat Word: {exc}")
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = ".docx"

    return FileResponse(
        path=output_path,
        filename=f"{safe_title}{ext}",
        media_type=media,
    )


@router.post("/documents/{doc_id}/generate-diagram")
async def generate_diagram(
    doc_id: uuid.UUID,
    instruction: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI menyusun syntax Mermaid dari instruksi user (konteks dokumen)."""
    doc = await _get_owned_doc(db, doc_id, current_user)
    if not instruction.strip():
        raise HTTPException(status_code=422, detail="Instruksi kosong.")

    llm = await _resolve_llm(db, current_user.id)
    from app.services.agentic_writer import AgenticWriter

    writer = AgenticWriter(llm)
    prompt = (
        "Kamu adalah pembuat diagram Mermaid untuk dokumen jurnal ilmiah.\n"
        f"Konteks dokumen saat ini:\n---\n{(await _sumber_tex_proyek(db, doc))[:6000]}\n---\n\n"
        f"Instruksi user: {instruction}\n\n"
        "Buat diagram Mermaid (flowchart/sequence/class/er/state sesuai kebutuhan). "
        "Balas HANYA dengan blok kode mermaid:\n```mermaid\n...\n```\n"
        "Tanpa teks lain."
    )
    try:
        raw = await writer.ask(prompt, system="Kamu adalah ahli Mermaid. Hanya keluarkan blok mermaid.")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI gagal: {exc}")

    mermaid = re.sub(r"```mermaid\s*|\s*```", "", raw, flags=re.IGNORECASE).strip()
    if not mermaid:
        raise HTTPException(status_code=502, detail="AI tidak menghasilkan diagram.")
    return {"mermaid": mermaid}


@router.post("/documents/{doc_id}/insert-media")
async def insert_media(
    doc_id: uuid.UUID,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """AI memetakan instruksi penempatan â†’ posisi heading yang tepat (preview).

    Body: {"instruction": "masukkan diagram di bawah Bab 3", "mermaid": "..."}
    Mengembalikan PREVIEW posisi â€” belum diterapkan (alur Terima/Tolak/Edit).
    """
    doc = await _get_owned_doc(db, doc_id, current_user)
    instruction = str(payload.get("instruction", "")).strip()
    mermaid = str(payload.get("mermaid", "")).strip()
    if not instruction or not mermaid:
        raise HTTPException(status_code=422, detail="Instruksi & mermaid wajib diisi.")

    # `insert_after_line` yang dikembalikan adalah indeks baris pada naskah yang
    # sedang dibuka klien, jadi yang dipindai harus naskah itu juga â€” bukan versi
    # terdatar, yang indeksnya tidak cocok dengan editor. Klien mengirim isi
    # berkas aktifnya lewat payload; tanpa itu jatuh ke main.tex.
    isi_aktif = payload.get("content")
    if not (isinstance(isi_aktif, str) and isi_aktif.strip()):
        isi_aktif = doc.content or ""
    lines = isi_aktif.splitlines()
    headings = [
        (i, judul)
        for i, line in enumerate(lines)
        if (judul := judul_dari_latex(line))
    ]
    if not headings:
        raise HTTPException(status_code=422, detail="Dokumen belum punya heading â€” buat struktur dulu.")

    llm = await _resolve_llm(db, current_user.id)
    from app.services.agentic_writer import AgenticWriter

    writer = AgenticWriter(llm)
    heading_list = "\n".join(f"{i}: {h}" for i, h in headings)
    prompt = (
        "Dokumen punya heading-index berikut:\n"
        f"{heading_list}\n\n"
        f"Instruksi user: {instruction}\n\n"
        "Tentukan heading INDEX tempat diagram harus disisipkan (sebelum heading itu). "
        "Balas HANYA dengan angka index, tanpa teks lain."
    )
    try:
        raw = await writer.ask(prompt, system="Kamu adalah parser posisi dokumen. Balas hanya angka index heading.")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI gagal: {exc}")

    try:
        target_idx = int(raw.strip().split()[0])
    except (ValueError, IndexError):
        # Fallback: sisipkan setelah heading terakhir (paling aman)
        target_idx = headings[-1][0]

    # Preview: blok mermaid + caption
    block = f"```mermaid\n{mermaid}\n```\n*Gambar â€” {instruction}*"
    return {
        "target_heading": next((h for i, h in headings if i == target_idx), None),
        "preview": block,
        "insert_after_line": target_idx,
        "confirm_required": True,
    }


@router.get("/learning-space", response_model=LearningSpaceData)
async def learning_space_data(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LearningSpaceData:
    """Data Ruang Riset yang bisa ditarik Co-Writer tanpa upload ulang:
    grup laporan, referensi, sitasi tersimpan, histori chat, dan draf.
    """
    from app.models.chat_history import ChatHistory
    from app.models.chat_session import ChatSession
    from app.models.journal import CitationCategory, JournalGroup, JournalReference, SavedCitation

    groups = (
        await db.scalars(
            select(JournalGroup).where(JournalGroup.user_id == current_user.id).order_by(JournalGroup.created_at.asc())
        )
    ).all()
    references = (
        await db.scalars(
            select(JournalReference).where(JournalReference.user_id == current_user.id).order_by(JournalReference.created_at.desc())
        )
    ).all()
    categories = (
        await db.scalars(
            select(CitationCategory).where(CitationCategory.user_id == current_user.id).order_by(CitationCategory.created_at.asc())
        )
    ).all()
    saved = (
        await db.scalars(
            select(SavedCitation).where(SavedCitation.user_id == current_user.id).order_by(SavedCitation.created_at.desc())
        )
    ).all()
    sessions = (
        await db.scalars(
            select(ChatSession).where(ChatSession.user_id == current_user.id).order_by(ChatSession.updated_at.desc()).limit(30)
        )
    ).all()
    drafts = (
        await db.scalars(
            select(CoWriterDocument).where(CoWriterDocument.user_id == current_user.id).order_by(CoWriterDocument.updated_at.desc()).limit(50)
        )
    ).all()

    return LearningSpaceData(
        groups=[
            {"id": str(g.id), "name": g.name, "description": g.description, "created_at": _epoch(g.created_at)}
            for g in groups
        ],
        references=[
            {
                "id": str(r.id),
                "group_id": str(r.group_id),
                "filename": r.filename,
                "title": r.title,
                "authors": r.authors or [],
                "year": r.year,
                "journal_name": r.journal_name,
                "status": r.status,
                "created_at": _epoch(r.created_at),
            }
            for r in references
        ],
        saved_citations=[
            {
                "id": str(c.id),
                "category_id": str(c.category_id),
                "category_name": next((cat.name for cat in categories if cat.id == c.category_id), ""),
                "reference_id": str(c.reference_id) if c.reference_id else None,
                "format": c.format,
                "citation_text": c.citation_text,
                "note": c.note,
                "created_at": _epoch(c.created_at),
            }
            for c in saved
        ],
        chat_history=[
            {
                "id": str(s.id),
                "title": s.title,
                "updated_at": _epoch(s.updated_at),
            }
            for s in sessions
        ],
        drafts=[
            {
                "id": str(d.id),
                "title": d.title,
                "updated_at": _epoch(d.updated_at),
                "preview": _preview(d.content),
            }
            for d in drafts
        ],
    )


@router.post("/import-chat", response_model=CoWriterDocumentOut)
async def import_chat_to_co_writer(
    payload: ImportChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CoWriterDocumentOut:
    """Ubah sesi chat menjadi bahan naskah jurnal LaTeX yang dapat dikompilasi.

    Sebelumnya endpoint ini menyimpan Markdown mentah ke editor LaTeX sehingga
    pratinjau PDF dapat gagal. Sekarang sesi dibungkus sebagai catatan riset
    terstruktur dan selalu dikonversi ke LaTeX sebelum disimpan.
    """
    from app.models.chat_history import ChatHistory
    from app.models.chat_session import ChatSession

    session = await db.scalar(
        select(ChatSession).where(
            ChatSession.id == payload.session_id, ChatSession.user_id == current_user.id
        )
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sesi chat tidak ditemukan.")

    messages = (
        await db.scalars(
            select(ChatHistory)
            .where(
                ChatHistory.session_id == session.id,
                ChatHistory.user_id == current_user.id,
            )
            .order_by(ChatHistory.created_at.asc())
        )
    ).all()

    chat_markdown = _chat_to_markdown(messages)
    research_notes = (
        f"# Bahan Naskah Jurnal: {session.title}\n\n"
        "## Catatan penggunaan\n\n"
        "Bagian ini berasal dari diskusi riset di chat. Verifikasi kembali setiap "
        "klaim, angka, dan referensi sebelum dipakai dalam naskah final.\n\n"
        "## Hasil Diskusi\n\n"
        f"{chat_markdown}\n\n"
        "## Kerangka Tindak Lanjut\n\n"
        "- Tetapkan rumusan masalah dan kontribusi utama.\n"
        "- Lengkapi bukti dan referensi untuk setiap klaim.\n"
        "- Susun naskah mengikuti Pendahuluan, Metode, Hasil, dan Pembahasan.\n"
        "- Jalankan pemeriksaan sitasi dan simulasi reviewer sebelum submit."
    )

    if payload.doc_id is not None:
        doc = await _get_owned_doc(db, payload.doc_id, current_user)
        addition = pastikan_latex(research_notes)
        source = _sumber_tex(doc)
        end_marker = r"\end{document}"
        end_index = source.rfind(end_marker)
        if end_index >= 0:
            doc.content = (
                source[:end_index].rstrip()
                + "\n\n"
                + addition
                + "\n\n"
                + source[end_index:]
            )
        else:
            doc.content = source.rstrip() + "\n\n" + addition
        await db.commit()
        await db.refresh(doc)
        return _detail(doc)

    new_doc = CoWriterDocument(
        user_id=current_user.id,
        title=f"Naskah Jurnal: {session.title}",
        content=markdown_to_latex(research_notes),
    )
    db.add(new_doc)
    await db.commit()
    await db.refresh(new_doc)
    return _detail(new_doc)


# --------------------------------------------------------------------------- #
# Import file laporan ke Co-Writer (dari directory lokal)
# --------------------------------------------------------------------------- #


@router.post("/import-file", response_model=CoWriterDocumentOut, status_code=status.HTTP_201_CREATED)
async def import_file_to_co_writer(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
) -> CoWriterDocumentOut:
    """Import file laporan (.tex/.md/.txt/.docx/.pdf) sebagai draf baru di Co-Writer.

    Isi file dibaca (DOCX/PDF diekstrak teksnya), judul diturunkan dari nama
    file, lalu disimpan sebagai dokumen Co-Writer milik user. Draf disimpan
    sebagai LaTeX; berkas `.tex` masuk apa adanya, format lain dikonversi.
    """
    import os

    filename = os.path.basename(file.filename or "laporan.tex")[:255]
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".tex", ".md", ".markdown", ".txt", ".docx", ".pdf"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Format tidak didukung: {ext or '(tanpa ekstensi)'}. "
                "Gunakan .tex, .md, .txt, .docx, atau .pdf."
            ),
        )

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File terlalu besar (maks 10 MB).")

    # â”€â”€ Import dengan preservasi struktur & gambar (PRD pipeline import) â”€â”€
    from starlette.concurrency import run_in_threadpool

    from app.services.doc_ast import markdown_to_ast
    from app.services.doc_import import docx_to_markdown, pdf_to_markdown
    new_doc = CoWriterDocument(
        user_id=current_user.id,
        title=os.path.splitext(filename)[0] or "Laporan diimpor",
        content="",
    )
    db.add(new_doc)
    # Baris kosongnya langsung disimpan, bukan sekadar di-flush. Ekstraksi di
    # bawah memakan puluhan detik, dan transaksi tulis yang menganggur selama
    # itu mengunci seluruh basis data SQLite sehingga impor lain yang berjalan
    # bersamaan gagal dengan "database is locked".
    await db.commit()
    await db.refresh(new_doc)

    images_dir = os.path.join("uploads", str(new_doc.id), "images")
    # Media diakses lewat route statis /uploads/ di backend yang sama
    media_base = f"{settings.backend_base_url}/uploads/{new_doc.id}/images"

    # Ekstraksi PDF/DOCX memakai analisis tata letak yang berat (beberapa detik
    # per halaman) dan sepenuhnya sinkron; dijalankan di threadpool supaya tidak
    # membekukan event loop selama impor berlangsung.
    if ext == ".docx":
        try:
            text = await run_in_threadpool(
                docx_to_markdown, contents, images_dir, media_base, str(new_doc.id)
            )
        except Exception as exc:  # noqa: BLE001
            await db.delete(new_doc)
            await db.commit()
            raise HTTPException(status_code=400, detail=f"Gagal membaca DOCX: {exc}")
    elif ext == ".pdf":
        # Pipeline baru (PRD P0): PDF -> DOCX terstruktur (pdf2docx, fallback
        # LibreOffice) -> post-process heading/cover -> DOCX kerja editor.
        # Berbeda dari jalur lama (assembly manual PyMuPDF yang merusak layout),
        # hasilnya DOCX valid dengan heading, tabel, dan gambar terjaga.
        text = ""  # jaring pengaman: semua jalur harus meng-assign text
        try:
            from app.services.pdf_docx_import import convert_pdf_to_docx
            from app.services.docx_postprocess import postprocess_docx, post_process_converted_docx

            pdf_tmp = Path(_document_upload_dir(new_doc.id)) / "sumber.pdf"
            pdf_tmp.parent.mkdir(parents=True, exist_ok=True)
            pdf_tmp.write_bytes(contents)

            docx_kerja = _onlyoffice_docx_path(new_doc.id)
            ok, res = await run_in_threadpool(convert_pdf_to_docx, pdf_tmp, docx_kerja)
            if not ok:
                await db.delete(new_doc)
                await db.commit()
                shutil.rmtree(_document_upload_dir(new_doc.id), ignore_errors=True)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Gagal mengonversi PDF ke DOCX. "
                        "Jika ini PDF hasil scan (foto/halaman gambar tanpa teks), "
                        "coba PDF lain yang teksnya bisa dipilih (copy), "
                        "atau gunakan fitur OCR bila tersedia. "
                        f"Detail teknis: {res.error}"
                    ),
                )

            await run_in_threadpool(postprocess_docx, docx_kerja)
            # Refine P1: koreksi bold via font-flags PDF asli + spacing dot-leader
            await run_in_threadpool(post_process_converted_docx, docx_kerja, pdf_tmp)

            # DOCX kerja (sudah valid) dijadikan sumber markdown via markitdown
            # untuk konten editor (mode sumber). markitdown menghasilkan
            # paragraf tersambung & tabel markdown yang jauh lebih rapi daripada
            # pandoc/PyMuPDF manual; fallback ke pandoc bila tidak tersedia.
            from app.services.doc_import import pdf_docx_to_markdown

            text = await run_in_threadpool(
                pdf_docx_to_markdown, docx_kerja, contents, images_dir, media_base, str(new_doc.id)
            )
            # Deteksi PDF scan/image-only: konversi berhasil tapi tanpa teks
            # yang bisa dibaca — beri tahu user dengan jelas, bukan diam-diam
            # menyimpan dokumen kosong.
            if not (text or "").strip():
                await db.delete(new_doc)
                await db.commit()
                shutil.rmtree(_document_upload_dir(new_doc.id), ignore_errors=True)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "PDF ini sepertinya hasil scan tanpa teks yang bisa dibaca. "
                        "Coba PDF lain, atau gunakan fitur OCR (jika tersedia)."
                    ),
                )
            res_detail = res.detail or ""
            log_import_pdf_conversion(new_doc.id, res)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            await db.delete(new_doc)
            await db.commit()
            shutil.rmtree(_document_upload_dir(new_doc.id), ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Gagal membaca PDF: {exc}")
    else:
        text = contents.decode("utf-8", errors="replace")

    try:
        _store_source_file(new_doc.id, ext, contents)
    except OSError as exc:
        await db.delete(new_doc)
        await db.commit()
        shutil.rmtree(_document_upload_dir(new_doc.id), ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Gagal menyimpan berkas sumber: {exc}")

    # Markdown-first: draf disimpan sebagai Markdown (sumber kebenaran).
    # Pengekstrak DOCX/PDF (pandoc) sudah menghasilkan Markdown â€” disimpan apa
    # adanya. Berkas `.tex` dikonversi SEKALI ke Markdown supaya seluruh dokumen
    # seragam; editor, pratinjau, dan ekspor tidak lagi bercabang dua format.
    new_doc.content = (
        latex_to_markdown(text or "")
        if ext == ".tex"
        else (text or "")
    )
    new_doc.content_format = "markdown"
    # Lapis 1 (PRD v2.8 Â§2, AST01): AST dibangun di perbatasan impor dan
    # disimpan sebagai JSON untuk navigasi outline "Pratinjau Rapi" â€” hasil
    # ekstraksi yang sudah dirapikan, bukan teks mentah.
    if new_doc.content:
        try:
            # AST dibangun dari hasil pembersihan penuh (listing kode di-fence,
            # nomor halaman terisolasi dibuang) â€” struktur, bukan teks mentah.
            from app.services.doc_import import _rapikan

            bersih = _rapikan(new_doc.content, preserve_content=False)
            ast = markdown_to_ast(bersih, title=new_doc.title)
            new_doc.structured_content = ast.to_json()
        except Exception:  # noqa: BLE001 â€” AST gagal bukan alasan impor gagal
            new_doc.structured_content = None
    await db.commit()
    await db.refresh(new_doc)

    # Auto-extract outline (PRD v2.4 Â§1): heading Markdown ("# ...").
    headings = [
        m.group(2).strip()
        for ln in (new_doc.content or "").splitlines()
        if (m := re.match(r"^[ \t]*(#{1,6})\s+(.+?)\s*$", ln))
    ]
    result = _detail(new_doc)
    result.outline = {
        "count": len(headings),
        "headings": headings[:50],
    }
    return result
