"""Agentic write untuk Co-Writer: AI membaca seluruh materi grup, menulis draf,
dan menyisipkan sitasi otomatis — dengan konfirmasi user sebelum diterapkan.

Alur:
1. User memberi perintah (mis. "tulis Bab 2 Tinjauan Pustaka dari jurnal grup Laporan A").
2. Sistem mengumpulkan metadata semua referensi grup + konteks RAG dari isi PDF.
3. LLM menulis draf dengan penanda sitasi [1], [2], dst (sesuai daftar referensi grup).
4. Sistem mengganti [n] dengan sitasi sesuai format pilihan user (IEEE/APA/dll).
5. Draf dikembalikan ke frontend — user MENYETUJUI dulu sebelum ditulis ke dokumen
   ("mau langsung dituliskan ke dokumen atau tidak?").
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.journal import JournalGroup, JournalReference
from app.models.user import User
from app.services.citation_formatter import (
    CitationError,
    citation_meta_from_reference,
    generate_citation,
)
from app.services.model_selection import ResolvedLLM

logger = logging.getLogger(__name__)

_MAX_REFERENCE_CHARS = 6000


def _reference_block(references: list[JournalReference]) -> str:
    """Daftar referensi bernomor untuk prompt (dipakai LLM sebagai daftar sitasi)."""
    lines = []
    for i, ref in enumerate(references, start=1):
        authors = ", ".join(ref.authors) if ref.authors else "(tanpa penulis)"
        year = ref.year if ref.year else "(tanpa tahun)"
        journal = ref.journal_name or "(tanpa jurnal)"
        lines.append(f"[{i}] {authors} ({year}). {ref.title}. {journal}.")
    return "\n".join(lines)


def resolve_reference_ids(references: list[JournalReference], markers: list[str]) -> list[int]:
    """Cari nomor referensi yang disebut LLM (mis. [1], [2]) → index 0-based."""
    ids: list[int] = []
    for marker in markers:
        match = re.search(r"\d+", marker)
        if match:
            idx = int(match.group()) - 1
            if 0 <= idx < len(references) and idx not in ids:
                ids.append(idx)
    return ids


def replace_citation_markers(text: str, references: list[JournalReference], format_name: str) -> tuple[str, list[str]]:
    """Ganti [n] dengan sitasi format pilihan; kembalikan (teks, daftar sitasi terpakai)."""
    used: list[str] = []

    def _replacer(match: re.Match) -> str:
        marker = match.group(0)
        ids = resolve_reference_ids(references, [marker])
        if not ids:
            return marker  # biarkan bila bukan nomor referensi valid
        idx = ids[0]
        ref = references[idx]
        try:
            citation = generate_citation(citation_meta_from_reference(ref), format_name)
        except CitationError:
            citation = f"{ref.title} ({ref.year if ref.year else 't.t.'})"
        used.append(citation)
        return f"[{idx + 1}]"

    replaced = re.sub(r"\[(\d+)\]", _replacer, text)
    return replaced, used


# Batas atas anggaran saat percobaan ulang: cukup untuk jejak penalaran panjang
# plus jawaban, tapi tetap membatasi biaya satu permintaan chat.
_ANGGARAN_ULANG_MAKS = 6000


class AgenticWriter:
    """Pembungkus AsyncOpenAI untuk agentic write (pola CoWriterLLM)."""

    def __init__(self, llm: ResolvedLLM) -> None:
        self._client = AsyncOpenAI(
            base_url=llm.base_url,
            api_key=llm.api_key or "dummy",
            timeout=240.0,
        )
        self._model = llm.model_name

    async def _panggil(self, request: dict) -> tuple[str, str | None, int]:
        """Jalankan satu permintaan dan kembalikan (teks, finish_reason, aksara penalaran).

        Jejak penalaran ikut diukur karena gateway proyek ini mengirimnya sebagai
        delta terpisah (`reasoning_content`) yang tetap memakan `max_tokens`.
        Tanpa angka itu, balasan kosong tampak seperti model mati padahal
        anggaran tokennya habis untuk berpikir.
        """
        stream = await self._client.chat.completions.create(**request)
        parts: list[str] = []
        penalaran_aksara = 0
        finish: str | None = None
        async for chunk in stream:
            if not chunk.choices:
                continue
            pilihan = chunk.choices[0]
            if pilihan.finish_reason:
                finish = pilihan.finish_reason
            delta = pilihan.delta.content
            if delta:
                parts.append(delta)
            for atribut in ("reasoning_content", "reasoning"):
                jejak = getattr(pilihan.delta, atribut, None)
                if jejak:
                    penalaran_aksara += len(jejak)
        return "".join(parts).strip(), finish, penalaran_aksara

    async def ask(
        self,
        prompt: str,
        *,
        system: str,
        temperature: float = 0.4,
        max_tokens: int = 6000,
        images: list[str] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        user_content: str | list[dict] = prompt
        if images:
            user_content = [{"type": "text", "text": prompt}]
            user_content.extend(
                {"type": "image_url", "image_url": {"url": image}}
                for image in images
            )
        request: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if reasoning_effort:
            request["reasoning_effort"] = reasoning_effort
        hasil, finish, penalaran = await self._panggil(request)
        # Model penalaran bisa menghabiskan seluruh anggaran untuk berpikir dan
        # berhenti dengan finish_reason="length" tanpa satu kata jawaban. Terukur
        # pada model aktif proyek ini: 2.900 aksara penalaran, nol aksara jawaban,
        # pada max_tokens=800. Daftar kemampuan di pengaturan tidak bisa dipakai
        # sebagai penanda — model itu mendaftarkan diri sebagai ["text", "tools"].
        # Jadi penalarannya dideteksi dari perilaku, lalu dicoba ulang sekali
        # dengan anggaran lebih besar; model non-penalaran tak pernah masuk sini.
        if not hasil and finish == "length" and penalaran > 0:
            anggaran_baru = min(_ANGGARAN_ULANG_MAKS, max(max_tokens * 3, 2500))
            if anggaran_baru > max_tokens:
                logger.info(
                    "Anggaran %s token habis untuk penalaran (%s aksara) pada %s; "
                    "diulang dengan %s token.",
                    max_tokens,
                    penalaran,
                    self._model,
                    anggaran_baru,
                )
                request["max_tokens"] = anggaran_baru
                hasil, finish, penalaran = await self._panggil(request)
        if not hasil:
            logger.warning(
                "Model %s tidak menghasilkan teks jawaban (finish_reason=%s, "
                "max_tokens=%s, aksara penalaran=%s).",
                self._model,
                finish,
                request["max_tokens"],
                penalaran,
            )
        return hasil

    async def ask_stream(
        self,
        prompt: str,
        *,
        system: str,
        temperature: float = 0.4,
        max_tokens: int = 6000,
    ):
        """Streaming variant — yields delta chunks as they arrive."""
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


_SYSTEM_AGENTIC_WRITER = (
    "Kamu adalah asisten penulisan jurnal ilmiah yang teliti. Kamu menulis bagian "
    "laporan akademik berdasarkan daftar referensi yang diberikan. Wajib: "
    "- Sisipkan sitasi bernomor [1], [2], dst pada kalimat yang mengambil informasi "
    "  dari referensi tersebut (nomor sesuai daftar).\n"
    "- Jangan mengarang fakta, angka, atau kutipan yang tidak ada di bahan.\n"
    "- Tulis dalam Bahasa Indonesia akademik yang baik.\n"
    "- Balas HANYA kode LaTeX hasil tulisan, tanpa pembuka/penutup/komentar.\n"
    "Aturan LaTeX: judul memakai \\section{...}/\\subsection{...} (BUKAN tanda "
    "pagar #), tebal \\textbf{...}, miring \\textit{...}, daftar "
    "\\begin{itemize} dengan \\item. Escape \\% \\& \\_ \\#. JANGAN menulis "
    "\\documentclass atau \\begin{document} — draf sudah punya bagian itu."
)


async def agentic_write(
    db: AsyncSession,
    user: User,
    llm: ResolvedLLM,
    *,
    group_id: uuid.UUID,
    instruction: str,
    format_name: str,
    rag_context: str = "",
) -> dict:
    """Jalankan agentic write: baca referensi grup → LLM tulis → ganti [n] → kembalikan draf.

    Returns dict:
        draft: kode LaTeX hasil tulisan (belum diterapkan ke dokumen)
        references: daftar sitasi yang dipakai (list[str], urut kemunculan)
        citation_count: jumlah sitasi tersisip
    """
    group = await db.scalar(
        select(JournalGroup).where(
            JournalGroup.id == group_id, JournalGroup.user_id == user.id
        )
    )
    if group is None:
        raise ValueError("Grup laporan tidak ditemukan.")

    references = list(
        (
            await db.scalars(
                select(JournalReference)
                .where(
                    JournalReference.group_id == group_id,
                    JournalReference.user_id == user.id,
                )
                .order_by(JournalReference.created_at.asc())
            )
        ).all()
    )
    if not references:
        raise ValueError("Grup ini belum memiliki referensi jurnal. Upload jurnal dulu.")

    ref_block = _reference_block(references)
    context = rag_context[:_MAX_REFERENCE_CHARS] if rag_context else ""
    prompt = (
        f"Grup laporan: {group.name}\n"
        f"Perintah user: {instruction}\n\n"
        f"Daftar referensi yang tersedia (nomor = sitasi yang wajib dipakai):\n{ref_block}\n\n"
    )
    if context:
        prompt += (
            "Konteks isi jurnal (dari RAG — gunakan untuk menulis akurat, "
            "tetap sitasi dengan nomor di atas):\n"
            f"{context}\n\n"
        )
    prompt += (
        "Tulis bagian laporan sesuai perintah. Sertakan sitasi [n] pada kalimat yang "
        "mengambil informasi dari referensi bernomor n."
    )

    writer = AgenticWriter(llm)
    raw = await writer.ask(prompt, system=_SYSTEM_AGENTIC_WRITER)

    draft, used = replace_citation_markers(raw, references, format_name)
    return {
        "draft": draft,
        "references": used,
        "citation_count": len(used),
    }


async def agentic_write_stream(
    db: AsyncSession,
    user: User,
    llm: ResolvedLLM,
    *,
    group_id: uuid.UUID,
    instruction: str,
    format_name: str,
    rag_context: str = "",
):
    """Streaming variant — yields dict events:
    {"stage": "writing", "delta": "..."} saat AI mengetik,
    {"stage": "done", "draft": ..., "references": [...], "citation_count": n}.
    """
    group = await db.scalar(
        select(JournalGroup).where(
            JournalGroup.id == group_id, JournalGroup.user_id == user.id
        )
    )
    if group is None:
        raise ValueError("Grup laporan tidak ditemukan.")

    references = list(
        (
            await db.scalars(
                select(JournalReference)
                .where(
                    JournalReference.group_id == group_id,
                    JournalReference.user_id == user.id,
                )
                .order_by(JournalReference.created_at.asc())
            )
        ).all()
    )
    if not references:
        raise ValueError("Grup ini belum memiliki referensi jurnal. Upload jurnal dulu.")

    ref_block = _reference_block(references)
    context = rag_context[:_MAX_REFERENCE_CHARS] if rag_context else ""
    prompt = (
        f"Grup laporan: {group.name}\n"
        f"Perintah user: {instruction}\n\n"
        f"Daftar referensi yang tersedia (nomor = sitasi yang wajib dipakai):\n{ref_block}\n\n"
    )
    if context:
        prompt += (
            "Konteks isi jurnal (dari RAG — gunakan untuk menulis akurat, "
            "tetap sitasi dengan nomor di atas):\n"
            f"{context}\n\n"
        )
    prompt += (
        "Tulis bagian laporan sesuai perintah. Sertakan sitasi [n] pada kalimat yang "
        "mengambil informasi dari referensi bernomor n."
    )

    writer = AgenticWriter(llm)
    raw_parts: list[str] = []
    async for delta in writer.ask_stream(prompt, system=_SYSTEM_AGENTIC_WRITER):
        raw_parts.append(delta)
        yield {"stage": "writing", "delta": delta}

    raw = "".join(raw_parts)
    draft, used = replace_citation_markers(raw, references, format_name)
    yield {
        "stage": "done",
        "draft": draft,
        "references": used,
        "citation_count": len(used),
    }
