"""Sitasi otomatis agen: dari `cite_add` sampai Daftar Pustaka tercetak.

Berbeda dari `test_citation_tools.py` yang menguji tool-nya sendiri, berkas ini
menjalankan **loop agentic sungguhan** (`run_agent_stream`) dengan dispatcher,
sesi basis data, dan formatter yang nyata — hanya modelnya yang diskrip. Tanpa
ini, jalur "model memanggil cite_add → server membalas nomor → nomor itu ditulis
ke naskah → Daftar Pustaka terisi" tidak pernah dijalankan sebagai satu kesatuan;
tool bisa benar sendiri-sendiri tapi rangkaiannya putus.

Yang tidak bisa dibuktikan di sini: apakah model sungguhan *mau* memanggil
`cite_add` sesuai prompt. Itu perlu proxy LLM hidup — lihat `uji_agen_sitasi.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select

from app.db.session import AsyncSessionLocal
from app.models.journal import JournalGroup, JournalReference
from app.models.user import User
from app.services.agent_run import run_agent_stream
from app.services.citation_formatter import (
    citation_meta_from_reference,
    generate_citation,
)
from app.services.citation_tools import NAMA_GRUP_AGEN, referensi_urut


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- kerangka model palsu (pola sama dengan test_agentic_run_penalaran.py) --- #


@dataclass
class _Fungsi:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _PanggilanTool:
    index: int
    id: str | None = None
    type: str | None = "function"
    function: _Fungsi | None = None


@dataclass
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list | None = None


@dataclass
class _Pilihan:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _Chunk:
    choices: list
    usage: object | None = None


class _Aliran:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()


def _giliran_tool(nama: str, argumen: str) -> list[_Chunk]:
    return [
        _Chunk([_Pilihan(_Delta(tool_calls=[
            _PanggilanTool(0, f"tc_{nama}", "function", _Fungsi(nama, argumen))
        ]))]),
        _Chunk([_Pilihan(_Delta(), "tool_calls")]),
    ]


def _giliran_prosa(teks: str) -> list[_Chunk]:
    return [
        _Chunk([_Pilihan(_Delta(content=teks))]),
        _Chunk([_Pilihan(_Delta(), "stop")]),
    ]


class _Completions:
    """Memutar giliran terskrip; hasil tool DIBACA agar nomor sitasi dipakai nyata."""

    def __init__(self, giliran: list[list[_Chunk]]) -> None:
        self.giliran = giliran
        self.panggilan = 0
        self.pesan_tool: list[str] = []

    async def create(self, **request):
        # Rekam balasan tool supaya tes bisa memastikan nomor benar-benar dari server.
        for m in request.get("messages", []):
            if m.get("role") == "tool":
                self.pesan_tool.append(str(m.get("content", "")))
        idx = min(self.panggilan, len(self.giliran) - 1)
        self.panggilan += 1
        return _Aliran(self.giliran[idx])


class _Client:
    def __init__(self, completions: _Completions) -> None:
        class _Chat:
            pass

        self.chat = _Chat()
        self.chat.completions = completions


async def _user_uji(db) -> User:
    email = "uji-agen-sitasi@contoh.test"
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            username="uji-agen-sitasi",
            email=email,
            hashed_password="x",
            full_name="Uji Agen Sitasi",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    for g in await db.scalars(select(JournalGroup).where(JournalGroup.user_id == user.id)):
        await db.execute(delete(JournalReference).where(JournalReference.group_id == g.id))
        await db.execute(delete(JournalGroup).where(JournalGroup.id == g.id))
    await db.execute(delete(JournalReference).where(JournalReference.user_id == user.id))
    await db.commit()
    return user


@pytest.mark.anyio
async def test_agen_menyimpan_sumber_lalu_menulis_nomor_yang_diberi_server():
    """Rangkaian penuh: cite_add → nomor dari server → `[n]` di naskah → Daftar Pustaka."""
    async with AsyncSessionLocal() as db:
        user = await _user_uji(db)

        completions = _Completions([
            _giliran_tool("submit_plan", '{"tasks":["Tulis 2.2 dengan rujukan"]}'),
            _giliran_tool("cite_add", json.dumps({
                "title": "Attention Is All You Need",
                "authors": ["Ashish Vaswani", "Noam Shazeer"],
                "year": 2017,
                "journal": "NeurIPS",
                "doi": "10.48550/arXiv.1706.03762",
            })),
            _giliran_tool("cite_add", json.dumps({
                "title": "Retrieval-Augmented Generation for Knowledge-Intensive NLP",
                "authors": ["Patrick Lewis"],
                "year": 2020,
                "journal": "NeurIPS",
            })),
            # Nomor di bawah HARUS cocok dengan yang dikembalikan server ([1], [2]).
            _giliran_tool("doc_insert", json.dumps({
                "markdown": (
                    "## 2.2 Large Language Model\n\nArsitektur Transformer menjadi "
                    "dasar LLM modern [1]. Penambatan jawaban pada dokumen sumber "
                    "dilakukan dengan pendekatan RAG [2]."
                )
            })),
            _giliran_tool("set_task_status", '{"task_index":0,"status":"done"}'),
            _giliran_prosa("Sub-bab 2.2 ditulis dengan dua rujukan terverifikasi."),
        ])

        events = []
        async for line in run_agent_stream(
            _Client(completions),
            "uji/model",
            instruction="Tambahkan sub-bab 2.2 dengan rujukan nyata.",
            doc_context="# Bab 2\n\n## 2.1 Kajian Pustaka\n\nIsi awal.",
            db=db,
            user_id=user.id,
            mode="seimbang",
            allow_web=True,
            context_window=65_536,
        ):
            events.append(json.loads(line))

        nama = [e["event"] for e in events]
        assert "error" not in nama, events
        assert "plan" in nama

        # 1) Sumber benar-benar tersimpan, bukan hanya diklaim.
        daftar = await referensi_urut(db, user.id)
        assert len(daftar) == 2, daftar
        assert daftar[0].title == "Attention Is All You Need"

        # 2) Nomor yang dipakai model memang berasal dari balasan server.
        balasan = " ".join(completions.pesan_tool)
        assert '"sitasi": "[1]"' in balasan, balasan[:400]
        assert '"sitasi": "[2]"' in balasan, balasan[:400]

        # 3) Nomor yang ditulis ke naskah sah dan menunjuk sumber yang benar.
        markdown = "".join(
            str(e["data"]["args"].get("markdown", ""))
            for e in events
            if e["event"] == "tool_call" and e["data"]["name"] == "doc_insert"
        )
        nomor = sorted({int(n) for n in re.findall(r"\[(\d+)\]", markdown)})
        assert nomor == [1, 2], markdown
        assert all(1 <= n <= len(daftar) for n in nomor)

        # 4) Daftar Pustaka yang tercetak cocok dengan nomornya.
        entri1 = generate_citation(citation_meta_from_reference(daftar[0]), "ieee")
        entri2 = generate_citation(citation_meta_from_reference(daftar[1]), "ieee")
        assert "Attention Is All You Need" in entri1
        assert "Retrieval-Augmented" in entri2

        # 5) cite_add adalah tool server: tidak boleh diminta dieksekusi frontend.
        for e in events:
            if e["event"] == "tool_call" and e["data"]["name"] in ("cite_add", "cite_list"):
                assert e["data"]["fe"] is False, e


@pytest.mark.anyio
async def test_sumber_tanpa_penulis_ditolak_tanpa_mematikan_run():
    """Metadata tak lengkap ditolak sebagai `ok:false`, run tetap lanjut sampai tuntas.

    Penolakan harus memandu model (cari sumbernya dulu), bukan menghentikan
    pekerjaan atau menambah entri kosong ke Daftar Pustaka.
    """
    async with AsyncSessionLocal() as db:
        user = await _user_uji(db)

        completions = _Completions([
            _giliran_tool("submit_plan", '{"tasks":["Tulis 2.2"]}'),
            _giliran_tool("cite_add", '{"title":"Sumber Tanpa Penulis","authors":[]}'),
            _giliran_tool("doc_insert", '{"markdown":"## 2.2 Isi\\n\\nDitulis tanpa sitasi."}'),
            _giliran_tool("set_task_status", '{"task_index":0,"status":"done"}'),
            _giliran_prosa("Ditulis tanpa rujukan karena sumber tak terverifikasi."),
        ])

        events = []
        async for line in run_agent_stream(
            _Client(completions),
            "uji/model",
            instruction="Tambahkan sub-bab 2.2.",
            doc_context="# Bab 2",
            db=db,
            user_id=user.id,
            mode="seimbang",
            allow_web=True,
            context_window=65_536,
        ):
            events.append(json.loads(line))

        hasil_cite = [
            e["data"] for e in events
            if e["event"] == "tool_result" and e["data"]["name"] == "cite_add"
        ]
        assert hasil_cite and hasil_cite[0]["ok"] is False, events
        assert "ditolak" in hasil_cite[0]["summary"].lower()

        # Tidak ada entri kosong yang masuk perpustakaan.
        assert await referensi_urut(db, user.id) == []

        # Run tetap tuntas sampai ringkasan.
        nama = [e["event"] for e in events]
        assert "text" in nama and "error" not in nama, events
