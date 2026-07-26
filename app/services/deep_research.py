"""Pipeline Riset Mendalam.

Alurnya meniru cara orang meneliti: pecah topik jadi beberapa kata kunci,
cari di internet, baca sumber yang paling relevan, susun kerangka, lalu tulis
laporan bagian per bagian sambil mengutip sumber. Semua tahap memakai
konfigurasi model AI milik user sendiri (tabel ``model_configs``), jadi tidak
ada dependensi berat tambahan.

Fungsi ``run_research`` dipanggil dari BackgroundTasks; ia membuka session DB
sendiri dan memperbarui kolom progres setiap kali satu tahap selesai supaya
frontend bisa menampilkan status secara langsung.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.research_report import ResearchReport
from app.services.document_tools import fetch_webpage, search_web

logger = logging.getLogger(__name__)


# Profil kedalaman: berapa kueri pencarian, berapa halaman dibaca, berapa
# bagian laporan, dan target panjang tiap bagian.
DEPTH_PROFILES: dict[str, dict[str, int]] = {
    "ringkas": {"queries": 3, "pages": 4, "sections": 4, "words": 250},
    "standar": {"queries": 5, "pages": 7, "sections": 6, "words": 400},
    "mendalam": {"queries": 8, "pages": 12, "sections": 8, "words": 600},
}

# Jumlah karakter isi halaman yang disimpan sebagai bahan tulisan.
_PAGE_CHARS = 6000
# Batas konteks yang disuapkan ke model saat menulis satu bagian.
_CONTEXT_CHARS = 18000


# --------------------------------------------------------------------------- #
# Utilitas kecil
# --------------------------------------------------------------------------- #

def _clean_llm_text(raw: str) -> str:
    """Buang pagar markdown dan basa-basi pembuka dari balasan model."""
    text = (raw or "").strip()
    fence = re.match(r"^```(?:\w+)?\s*(.+?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return text


def _extract_json_list(raw: str) -> list[Any]:
    """Ambil array JSON dari balasan model; kembalikan list kosong bila gagal."""
    text = _clean_llm_text(raw)
    candidates = [text]
    if "[" in text and "]" in text:
        candidates.append(text[text.index("[") : text.rindex("]") + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("queries", "kueri", "sections", "bagian", "outline", "kerangka", "items"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
    return []


def _fallback_lines(raw: str, limit: int) -> list[str]:
    """Cadangan bila model membalas daftar biasa, bukan JSON."""
    lines = []
    for line in _clean_llm_text(raw).splitlines():
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip('"').strip()
        if len(cleaned) > 2:
            lines.append(cleaned)
        if len(lines) >= limit:
            break
    return lines


# --------------------------------------------------------------------------- #
# Pemanggilan model
# --------------------------------------------------------------------------- #

class _Writer:
    """Pembungkus tipis di atas AsyncOpenAI agar pemanggilan seragam."""

    def __init__(self, base_url: str, api_key: str, model_name: str) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "dummy", timeout=180.0)
        self._model = model_name

    async def ask(self, prompt: str, *, system: str, temperature: float = 0.4, max_tokens: int = 2000) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (response.choices[0].message.content or "").strip()


_SYSTEM_RESEARCHER = (
    "Kamu adalah peneliti akademik berbahasa Indonesia yang teliti. "
    "Tulis dengan gaya ilmiah populer: jelas, runtut, tanpa mengarang fakta. "
    "Jika informasi tidak ada di bahan yang diberikan, katakan bahwa data tersebut belum ditemukan."
)


# --------------------------------------------------------------------------- #
# Tahap-tahap pipeline
# --------------------------------------------------------------------------- #

async def _make_queries(writer: _Writer, topic: str, instructions: str | None, count: int) -> list[str]:
    """Tahap 1: pecah topik menjadi beberapa kata kunci pencarian."""
    extra = f"\nArahan tambahan dari pengguna: {instructions}" if instructions else ""
    prompt = (
        f"Topik riset: {topic}{extra}\n\n"
        f"Buat {count} kata kunci pencarian internet yang saling melengkapi untuk meneliti topik itu. "
        "Campur bahasa Indonesia dan Inggris agar cakupannya luas. "
        'Balas HANYA array JSON berisi string, contoh: ["kata kunci 1", "kata kunci 2"].'
    )
    raw = await writer.ask(prompt, system=_SYSTEM_RESEARCHER, temperature=0.5, max_tokens=500)
    queries = [str(q).strip() for q in _extract_json_list(raw) if str(q).strip()]
    if not queries:
        queries = _fallback_lines(raw, count)
    if not queries:
        queries = [topic]
    return queries[:count]


async def _collect_sources(queries: list[str], max_pages: int) -> list[dict[str, str]]:
    """Tahap 2: cari di internet lalu buka halaman-halaman teratas."""
    seen: set[str] = set()
    candidates: list[dict[str, str]] = []

    for query in queries:
        try:
            payload = json.loads(await search_web(query, max_results=5))
        except Exception as exc:
            logger.warning(f"Riset: pencarian '{query}' gagal: {exc}")
            continue
        for item in payload.get("results", []):
            url = (item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            candidates.append({
                "url": url,
                "title": (item.get("title") or url).strip(),
                "snippet": (item.get("body") or "").strip(),
                "query": query,
            })

    sources: list[dict[str, str]] = []
    for candidate in candidates:
        if len(sources) >= max_pages:
            break
        try:
            page = json.loads(await fetch_webpage(candidate["url"], max_chars=_PAGE_CHARS))
        except Exception as exc:
            logger.info(f"Riset: gagal membuka {candidate['url']}: {exc}")
            continue
        text = (page.get("text") or "").strip()
        if page.get("error") or len(text) < 400:
            # Halaman kosong/diblokir tidak berguna sebagai rujukan.
            continue
        sources.append({
            "url": page.get("url") or candidate["url"],
            "title": page.get("title") or candidate["title"],
            "snippet": candidate["snippet"],
            "text": text,
        })

    # Kalau semua halaman gagal dibuka, cuplikan hasil pencarian masih lebih
    # baik daripada tidak ada bahan sama sekali.
    if not sources:
        for candidate in candidates[:max_pages]:
            if candidate["snippet"]:
                sources.append({
                    "url": candidate["url"],
                    "title": candidate["title"],
                    "snippet": candidate["snippet"],
                    "text": candidate["snippet"],
                })
    return sources


async def _make_outline(
    writer: _Writer, topic: str, instructions: str | None, sources: list[dict[str, str]], count: int
) -> list[dict[str, str]]:
    """Tahap 3: susun kerangka laporan dari bahan yang terkumpul."""
    digest = "\n".join(
        f"[{i + 1}] {s['title']} — {(s['snippet'] or s['text'])[:300]}"
        for i, s in enumerate(sources)
    )
    extra = f"\nArahan tambahan: {instructions}" if instructions else ""
    prompt = (
        f"Topik riset: {topic}{extra}\n\n"
        f"Ringkasan sumber yang tersedia:\n{digest or '(tidak ada sumber daring, pakai pengetahuan umum)'}\n\n"
        f"Susun kerangka laporan berisi {count} bagian isi (tanpa pendahuluan dan kesimpulan, "
        "keduanya ditulis terpisah). Balas HANYA array JSON dengan bentuk "
        '[{"judul": "...", "fokus": "hal-hal yang harus dibahas di bagian ini"}].'
    )
    raw = await writer.ask(prompt, system=_SYSTEM_RESEARCHER, temperature=0.4, max_tokens=1200)

    outline: list[dict[str, str]] = []
    for item in _extract_json_list(raw):
        if isinstance(item, dict):
            judul = str(item.get("judul") or item.get("title") or item.get("heading") or "").strip()
            fokus = str(item.get("fokus") or item.get("focus") or item.get("deskripsi") or "").strip()
            if judul:
                outline.append({"judul": judul, "fokus": fokus})
        elif isinstance(item, str) and item.strip():
            outline.append({"judul": item.strip(), "fokus": ""})

    if not outline:
        outline = [{"judul": line, "fokus": ""} for line in _fallback_lines(raw, count)]
    if not outline:
        outline = [{"judul": f"Aspek {i + 1} dari {topic}", "fokus": ""} for i in range(count)]
    return outline[:count]


def _context_block(sources: list[dict[str, str]]) -> str:
    """Bahan bacaan yang disuapkan ke model, lengkap dengan nomor rujukan."""
    parts = []
    for i, s in enumerate(sources):
        parts.append(f"[{i + 1}] {s['title']} ({s['url']})\n{s['text'][:4000]}")
    return "\n\n---\n\n".join(parts)[:_CONTEXT_CHARS]


async def _write_section(
    writer: _Writer, topic: str, section: dict[str, str], context: str, target_words: int, position: str
) -> str:
    """Tahap 4: tulis satu bagian laporan."""
    fokus = f"\nFokus bahasan: {section['fokus']}" if section.get("fokus") else ""
    prompt = (
        f"Topik laporan: {topic}\n"
        f"Bagian yang ditulis sekarang: {section['judul']}{fokus}\n"
        f"Posisi bagian: {position}\n\n"
        f"Bahan rujukan (angka dalam kurung siku adalah nomor sumber):\n{context or '(tidak ada rujukan daring)'}\n\n"
        f"Tulis isi bagian ini sepanjang kurang lebih {target_words} kata dalam Bahasa Indonesia. "
        "Aturan:\n"
        "- Jangan tulis ulang judul bagian, langsung isi paragrafnya.\n"
        "- Sisipkan rujukan seperti [1] atau [2] pada kalimat yang mengambil informasi dari sumber.\n"
        "- Boleh memakai sub-judul '###' dan daftar poin bila membantu.\n"
        "- Jangan mengarang angka atau kutipan yang tidak ada di bahan."
    )
    body = await writer.ask(
        prompt, system=_SYSTEM_RESEARCHER, temperature=0.45, max_tokens=max(900, target_words * 3)
    )
    return _clean_llm_text(body)


def _bibliography(sources: list[dict[str, str]]) -> str:
    if not sources:
        return ""
    lines = ["## Daftar Pustaka", ""]
    for i, s in enumerate(sources):
        lines.append(f"{i + 1}. {s['title']}. Tersedia di: {s['url']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orkestrasi
# --------------------------------------------------------------------------- #

async def _set_progress(db, report: ResearchReport, step: str, percent: int) -> None:
    report.progress_step = step
    report.progress_percent = percent
    await db.commit()


async def run_research(
    *,
    report_id: str,
    base_url: str,
    api_key: str,
    model_name: str,
    db_url: str,
) -> None:
    """Jalankan seluruh pipeline riset untuk satu baris ``research_reports``.

    Dipanggil lewat BackgroundTasks, jadi ia membuka engine dan session DB
    sendiri (session milik request sudah ditutup saat fungsi ini berjalan).
    """
    engine = create_async_engine(db_url, echo=False)
    session_factory = async_sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    async with session_factory() as db:
        report = await db.get(ResearchReport, uuid.UUID(report_id))
        if report is None:
            await engine.dispose()
            return

        try:
            profile = DEPTH_PROFILES.get(report.depth, DEPTH_PROFILES["standar"])
            writer = _Writer(base_url=base_url, api_key=api_key, model_name=model_name)
            topic, instructions = report.topic, report.instructions

            report.status = "running"
            await _set_progress(db, report, "Menyusun kata kunci pencarian", 5)

            queries = await _make_queries(writer, topic, instructions, profile["queries"])
            await _set_progress(db, report, f"Mencari {len(queries)} kata kunci di internet", 15)

            sources = await _collect_sources(queries, profile["pages"])
            report.sources = [
                {"title": s["title"], "url": s["url"], "snippet": s["snippet"][:400]} for s in sources
            ]
            await _set_progress(db, report, f"Membaca {len(sources)} sumber", 30)

            outline = await _make_outline(writer, topic, instructions, sources, profile["sections"])
            report.outline = outline
            await _set_progress(db, report, "Menyusun kerangka laporan", 40)

            context = _context_block(sources)
            chunks: list[str] = [f"# {topic}", ""]

            intro = await _write_section(
                writer, topic,
                {"judul": "Pendahuluan", "fokus": "latar belakang, mengapa topik ini penting, dan cakupan pembahasan"},
                context, max(200, profile["words"] - 100), "pembuka laporan",
            )
            chunks += ["## Pendahuluan", "", intro, ""]
            await _set_progress(db, report, "Menulis pendahuluan", 50)

            total = len(outline)
            for i, section in enumerate(outline):
                body = await _write_section(
                    writer, topic, section, context, profile["words"], f"bagian isi ke-{i + 1} dari {total}"
                )
                chunks += [f"## {section['judul']}", "", body, ""]
                percent = 50 + int(40 * (i + 1) / max(total, 1))
                await _set_progress(db, report, f"Menulis bagian {i + 1}/{total}: {section['judul']}"[:255], percent)

            closing = await _write_section(
                writer, topic,
                {"judul": "Kesimpulan", "fokus": "rangkuman temuan utama, keterbatasan, dan saran riset lanjutan"},
                context, max(180, profile["words"] - 150), "penutup laporan",
            )
            chunks += ["## Kesimpulan", "", closing, ""]

            biblio = _bibliography(sources)
            if biblio:
                chunks += [biblio, ""]

            markdown = "\n".join(chunks).strip()
            report.content_markdown = markdown
            report.word_count = len(markdown.split())
            report.status = "completed"
            report.error_message = None
            await _set_progress(db, report, "Selesai", 100)

        except Exception as exc:
            logger.exception(f"Riset {report_id} gagal")
            report.status = "failed"
            report.error_message = str(exc)[:900]
            report.progress_step = "Gagal"
            await db.commit()

    await engine.dispose()
