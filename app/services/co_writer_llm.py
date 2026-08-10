"""Layanan AI untuk Co-Writer: edit draf penuh, auto-struktur LaTeX, dan edit seleksi streaming.

Pola pemanggilan model mengikuti ``deep_research``: konfigurasi LLM diresolusi
lewat ``model_selection.resolve_llm`` (profil katalog atau cadangan
``model_configs``), lalu dibungkus ``AsyncOpenAI``. Tidak ada dependensi baru.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import AsyncIterator

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument
from app.models.user import User
from app.services.document_tools import search_web
from app.services.model_selection import ResolvedEmbedding, ResolvedLLM
from app.services.rag import query_documents

logger = logging.getLogger(__name__)

# Batas panjang teks agar tidak meledakkan konteks model.
_MAX_SELECTED_CHARS = 12000
_MAX_DRAFT_CHARS = 40000


def _timeout_dari_env(bawaan: int = 240) -> int:
    """Batas waktu satu permintaan AI Co-Writer, detik.

    Bawaannya longgar karena model penalaran (reasoning) menghabiskan sebagian
    besar anggaran tokennya untuk berpikir sebelum satu kata jawaban keluar:
    pada model aktif proyek ini, menulis ulang satu kalimat saja butuh ~73 detik
    dengan 460 dari 500 token terpakai untuk penalaran. Batas 90 detik yang
    dipakai sebelumnya membuat tiap permintaan nyata berakhir 504.
    """
    mentah = os.getenv("CO_WRITER_REQUEST_TIMEOUT_SECONDS", "").strip()
    if not mentah:
        return bawaan
    try:
        nilai = int(float(mentah))
    except ValueError:
        return bawaan
    return nilai if nilai > 0 else bawaan


CO_WRITER_REQUEST_TIMEOUT_SECONDS = _timeout_dari_env()

_SYSTEM_CO_WRITER = (
    "Kamu adalah asisten penulisan akademik dan profesional yang mahir berbahasa "
    "Indonesia. Draf ditulis dalam LaTeX, jadi balas HANYA dengan kode LaTeX untuk "
    "badan dokumen — tanpa pembuka, tanpa penutup, tanpa pagar ```, tanpa komentar.\n"
    "Aturan LaTeX:\n"
    "- Judul memakai \\section{...}, \\subsection{...}, \\subsubsection{...}. "
    "JANGAN memakai tanda pagar (#) gaya Markdown.\n"
    "- Tebal \\textbf{...}, miring \\textit{...}, kode \\texttt{...}. "
    "JANGAN memakai **teks** atau *teks*.\n"
    "- Daftar memakai \\begin{itemize}/\\begin{enumerate} dengan \\item.\n"
    "- Tabel memakai \\begin{tabular} dengan & sebagai pemisah sel dan \\\\ akhir baris.\n"
    "- Escape karakter khusus: \\% \\& \\_ \\# untuk persen, dan, garis bawah, pagar.\n"
    "- JANGAN menulis \\documentclass, \\usepackage, \\begin{document}, atau "
    "\\end{document} — bagian itu sudah ada di draf."
)

_ACTION_PROMPTS: dict[str, str] = {
    "rewrite": (
        "Tulis ulang seluruh draf sesuai instruksi pengguna. Pertahankan struktur "
        "dan perintah LaTeX yang ada beserta faktanya; perbaiki alur, diksi, dan "
        "keruntutan."
    ),
    "shorten": (
        "Ringkas seluruh draf secara signifikan: buang pengulangan dan kalimat "
        "bertele-tele, pertahankan semua poin penting, judul, dan struktur LaTeX."
    ),
    "expand": (
        "Kembangkan seluruh draf: tambahkan detail, contoh, dan penjelasan yang "
        "relevan di tiap bagian tanpa mengubah fakta atau menambah klaim baru."
    ),
}

_MODE_PROMPTS: dict[str, str] = {
    "none": "Ikuti instruksi pengguna pada teks terpilih.",
    "shorten": "Ringkas teks terpilih: buang pengulangan, pertahankan poin penting.",
    "expand": "Kembangkan teks terpilih: tambahkan detail, contoh, dan penjelasan relevan.",
    "rewrite": "Tulis ulang teks terpilih sesuai instruksi pengguna.",
}

_SYSTEM_AUTOMARK = (
    "Kamu adalah editor LaTeX yang teliti. Beri struktur LaTeX pada teks mentah "
    "berikut: tentukan judul (\\section), subjudul (\\subsection/\\subsubsection), "
    "tebal (\\textbf{...}), miring (\\textit{...}), dan daftar "
    "(\\begin{itemize}/\\begin{enumerate} dengan \\item) pada bagian yang memang "
    "layak. Escape karakter khusus: \\% \\& \\_ \\#.\n"
    "JANGAN mengubah isi kalimat atau menambah/menghapus informasi — hanya "
    "tambahkan perintah LaTeX. JANGAN menulis \\documentclass atau "
    "\\begin{document}. Balas HANYA kode LaTeX hasil, tanpa komentar."
)


def _clean_llm_text(raw: str) -> str:
    """Buang pagar kode dan spasi berlebih dari balasan model."""
    text = (raw or "").strip()
    fence = re.match(r"^```(?:latex|tex|markdown|md|text)?\s*(.+?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return text


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


class CoWriterLLM:
    """Pembungkus AsyncOpenAI untuk pemanggilan seragam (mirip deep_research)."""

    def __init__(self, llm: ResolvedLLM) -> None:
        self._client = AsyncOpenAI(
            base_url=llm.base_url,
            api_key=llm.api_key or "dummy",
            timeout=float(CO_WRITER_REQUEST_TIMEOUT_SECONDS),
            max_retries=0,
        )
        self._model = llm.model_name

    async def ask(
        self,
        prompt: str,
        *,
        system: str = _SYSTEM_CO_WRITER,
        temperature: float = 0.4,
        max_tokens: int = 8000,
    ) -> str:
        """Panggil model dan kumpulkan jawaban penuh.

        Selalu memakai ``stream=True``: beberapa endpoint OpenAI-compatible
        (mis. gateway lokal yang dipakai proyek ini) mengembalikan SSE chunks
        bahkan saat stream=False, yang membuat SDK menggantung menunggu JSON.
        """
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        parts: list[str] = []
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                parts.append(delta)
        return _clean_llm_text("".join(parts))

    async def ask_stream(
        self,
        prompt: str,
        *,
        system: str = _SYSTEM_CO_WRITER,
        temperature: float = 0.4,
        max_tokens: int = 8000,
    ) -> AsyncIterator[str]:
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


# --------------------------------------------------------------------------- #
# Konteks tambahan (tool web / rag)
# --------------------------------------------------------------------------- #


async def resolve_kb_document_ids(
    db: AsyncSession,
    user_id,
    kb_name: str | None,
) -> list[str]:
    """Resolusi nama knowledge base menjadi daftar document_id.

    Bila nama tidak diberikan, pakai KB default; bila tidak ada, pakai KB
    pertama milik user. Kosong bila user belum punya KB sama sekali.
    """
    stmt = select(KnowledgeBase).where(KnowledgeBase.user_id == user_id)
    if kb_name:
        stmt = stmt.where(KnowledgeBase.name == kb_name)
    else:
        stmt = stmt.order_by(KnowledgeBase.is_default.desc(), KnowledgeBase.created_at.asc())
    kb = await db.scalar(stmt.limit(1))
    if kb is None:
        return []

    rows = await db.scalars(
        select(KnowledgeBaseDocument.document_id).where(KnowledgeBaseDocument.kb_id == kb.id)
    )
    return [str(doc_id) for doc_id in rows.all() if doc_id]


async def gather_web_context(
    query: str,
    trace,
) -> str:
    """Cari di internet, kembalikan teks konteks untuk prompt."""
    try:
        await trace("tool_call", "web.search", {"query": _truncate(query, 300)})
        raw = await search_web(query, max_results=5)
        try:
            parsed = json.loads(raw)
            results = parsed.get("results") or []
        except (ValueError, TypeError):
            results = []
        if not results:
            await trace("tool_result", "web.search", {"tool": "web.search"}, success=False, result=raw[:500])
            return ""
        blocks = []
        for r in results[:5]:
            title = r.get("title", "")
            body = r.get("body", "")
            url = r.get("url", "")
            blocks.append(f"- {title}\n  {body}\n  Sumber: {url}")
        context = "\n".join(blocks)
        await trace("tool_result", "web.search", {"tool": "web.search", "count": len(results)}, success=True, result=_truncate(context, 1500))
        return context
    except Exception as exc:  # noqa: BLE001 — tool tidak boleh mematikan alur utama
        logger.warning("co_writer web tool gagal: %s", exc)
        await trace("tool_result", "web.search", {"tool": "web.search"}, success=False, result=str(exc)[:300])
        return ""


async def gather_rag_context(
    db: AsyncSession,
    user: User,
    llm: ResolvedLLM,
    embedding: ResolvedEmbedding | None,
    query: str,
    kb_name: str | None,
    trace,
) -> str:
    """Ambil konteks dari knowledge base via ChromaDB (RAG)."""
    try:
        document_ids = await resolve_kb_document_ids(db, user.id, kb_name)
        if not document_ids:
            await trace("tool_result", "rag.query", {"tool": "rag.query"}, success=False, result="Tidak ada knowledge base / dokumen terindeks.")
            return ""
        if embedding is None:
            await trace("tool_result", "rag.query", {"tool": "rag.query"}, success=False, result="Model embedding belum dikonfigurasi.")
            return ""

        await trace("tool_call", "rag.query", {"query": _truncate(query, 300), "documents": len(document_ids)})
        result = await query_documents(
            str(user.id),
            query,
            llm.base_url,
            llm.api_key or "dummy",
            llm.model_name,
            embedding.model_name,
            document_ids,
            top_k=4,
            embedding_base_url=embedding.base_url,
            embedding_api_key=embedding.api_key or "dummy",
        )
        answer = (result.get("answer") or "").strip()
        sources = result.get("sources") or []
        context = answer
        if sources:
            excerpts = [
                f"- {s.get('filename', '')}: {s.get('excerpt', '')[:400]}"
                for s in sources[:4]
            ]
            context = f"{answer}\n\nKutipan sumber:\n" + "\n".join(excerpts)
        await trace(
            "tool_result", "rag.query",
            {"tool": "rag.query", "sources": len(sources)},
            success=True,
            result=_truncate(context, 1500),
        )
        return context
    except Exception as exc:  # noqa: BLE001
        logger.warning("co_writer rag tool gagal: %s", exc)
        await trace("tool_result", "rag.query", {"tool": "rag.query"}, success=False, result=str(exc)[:300])
        return ""


# --------------------------------------------------------------------------- #
# Penyusun prompt
# --------------------------------------------------------------------------- #


def build_full_edit_prompt(
    text: str,
    instruction: str,
    action: str,
    context: str,
) -> tuple[str, str]:
    system = _SYSTEM_CO_WRITER
    action_line = _ACTION_PROMPTS.get(action, _ACTION_PROMPTS["rewrite"])
    user_parts = [
        f"Tugas: {action_line}",
        f"Instruksi pengguna: {instruction.strip() or '(tidak ada instruksi tambahan)'}",
    ]
    if context:
        user_parts.append(
            "Konteks pendukung (gunakan bila relevan, jangan menyalin mentah):\n"
            + _truncate(context, 12000)
        )
    user_parts.append(
        "Draf saat ini (LaTeX):\n\n" + _truncate(text, _MAX_DRAFT_CHARS)
    )
    user_parts.append(
        "\n\nBalas HANYA kode LaTeX hasil akhir, tanpa komentar atau pembuka."
    )
    return system, "\n\n".join(user_parts)


def build_automark_prompt(text: str) -> tuple[str, str]:
    return _SYSTEM_AUTOMARK, _truncate(text, _MAX_DRAFT_CHARS)


def build_selection_prompt(
    selected_text: str,
    instruction: str,
    mode: str,
    context: str,
) -> tuple[str, str]:
    system = _SYSTEM_CO_WRITER
    mode_line = _MODE_PROMPTS.get(mode, _MODE_PROMPTS["rewrite"])
    user_parts = [f"Tugas: {mode_line}"]
    if instruction.strip():
        user_parts.append(f"Instruksi pengguna: {instruction.strip()}")
    if context:
        user_parts.append(
            "Konteks pendukung (gunakan bila relevan, jangan menyalin mentah):\n"
            + _truncate(context, 12000)
        )
    user_parts.append(
        "Teks terpilih (LaTeX):\n\n" + _truncate(selected_text, _MAX_SELECTED_CHARS)
    )
    user_parts.append(
        "\n\nBalas HANYA kode LaTeX untuk menggantikan teks terpilih, "
        "tanpa komentar atau pembuka."
    )
    return system, "\n\n".join(user_parts)
