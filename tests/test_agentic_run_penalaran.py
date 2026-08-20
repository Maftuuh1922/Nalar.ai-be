"""Loop agentic Co-Writer pada model penalaran yang menghabiskan anggaran token.

Cacat yang diuji di sini membuat panel "Asisten Agentic" tampak mati: model aktif
proyek ini menghabiskan seluruh ``max_tokens`` untuk jejak penalaran lalu berhenti
dengan ``finish_reason="length"`` — nol aksara jawaban, nol tool call. Karena
``plan_submitted`` masih False, cabang "dorong lanjut" dilewati dan loop langsung
``break``, sehingga run berakhir hanya dengan event ``['reasoning', 'end']``.
Frontend menerima ``end`` (jadi penjaga "koneksi terputus" tidak menyala) dan
menampilkan panel sunyi tanpa rencana, tanpa error, tanpa ringkasan.

``agent_run.py`` sudah menangkap ``finish_reason`` tetapi tidak pernah memakainya,
padahal dua layanan saudara sudah menanganinya (``agentic_writer.ask`` mengulang
dengan anggaran lebih besar; ``agentic_chat`` memancarkan event ``truncated``).

Cacat kedua: argumen tool yang terpotong di tengah JSON menjadi ``{}`` secara
senyap, lalu ``doc_insert`` melaporkan "Menyisipkan 0 karakter" sebagai SUKSES —
model menandai tugasnya 'done' dan lanjut, padahal dokumen tidak berubah.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.services.agent_run import _anggaran_awal, run_agent_stream


@pytest.fixture
def anyio_backend():
    return "asyncio"


# Jendela konteks model aktif proyek ini; anggaran keluaran dibatasi seperempatnya
# sehingga percobaan ulang benar-benar punya ruang untuk naik.
_CONTEXT_WINDOW = 65_536


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
    tool_calls: list[_PanggilanTool] | None = None


@dataclass
class _Pilihan:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _Chunk:
    choices: list[_Pilihan]
    usage: object | None = None


class _AliranPalsu:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


def _giliran_penalaran_habis(aksara: int = 2900) -> list[_Chunk]:
    """Anggaran habis untuk berpikir: hanya jejak penalaran, lalu 'length'."""
    return [
        _Chunk([_Pilihan(_Delta(reasoning_content="p" * aksara))]),
        _Chunk([_Pilihan(_Delta(), "length")]),
    ]


def _giliran_tool(nama: str, argumen: str, finish: str = "tool_calls") -> list[_Chunk]:
    return [
        _Chunk([_Pilihan(_Delta(tool_calls=[
            _PanggilanTool(0, f"tc_{nama}", "function", _Fungsi(nama, argumen))
        ]))]),
        _Chunk([_Pilihan(_Delta(), finish)]),
    ]


def _giliran_prosa(teks: str) -> list[_Chunk]:
    return [
        _Chunk([_Pilihan(_Delta(content=teks))]),
        _Chunk([_Pilihan(_Delta(), "stop")]),
    ]


class _CompletionsPalsu:
    """Memutar giliran terskrip; giliran terakhir diulang bila skrip habis."""

    def __init__(self, giliran: list[list[_Chunk]]) -> None:
        self.anggaran: list[int] = []
        self._giliran = giliran

    async def create(self, **request):
        self.anggaran.append(request["max_tokens"])
        idx = min(len(self.anggaran) - 1, len(self._giliran) - 1)
        return _AliranPalsu(self._giliran[idx])


class _ClientPalsu:
    def __init__(self, completions: _CompletionsPalsu) -> None:
        class _Chat:
            pass

        self.chat = _Chat()
        self.chat.completions = completions


async def _jalankan(completions: _CompletionsPalsu, **kwargs) -> list[dict]:
    events: list[dict] = []
    async for line in run_agent_stream(
        _ClientPalsu(completions),
        "uji/model-penalaran",
        instruction="Tulis Bab 1 Pendahuluan",
        doc_context="# Judul\n\nKalimat pembuka dokumen.",
        db=None,
        user_id=None,
        mode="cepat",
        allow_web=False,
        context_window=_CONTEXT_WINDOW,
        **kwargs,
    ):
        events.append(json.loads(line))
    return events


def _nama_event(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


@pytest.mark.anyio
async def test_anggaran_habis_untuk_penalaran_diulang_dengan_anggaran_lebih_besar():
    completions = _CompletionsPalsu([
        _giliran_penalaran_habis(),
        _giliran_tool("submit_plan", '{"tasks":["Tulis 1.1","Tulis 1.2"]}'),
        _giliran_prosa("Rencana dijalankan."),
    ])

    events = await _jalankan(completions)

    assert "plan" in _nama_event(events), "rencana harus tetap terbit setelah diulang"
    assert len(completions.anggaran) >= 2, "harus mengulang panggilan"
    assert completions.anggaran[1] > completions.anggaran[0], (
        f"anggaran harus naik saat penalaran menghabiskannya: {completions.anggaran}"
    )
    assert completions.anggaran[0] == _anggaran_awal(_CONTEXT_WINDOW)


@pytest.mark.anyio
async def test_tetap_kosong_setelah_diulang_memancarkan_error_bukan_diam():
    """Regresi utama: run tidak boleh berakhir hanya ['reasoning', 'end']."""
    completions = _CompletionsPalsu([_giliran_penalaran_habis()])

    events = await _jalankan(completions)

    nama = _nama_event(events)
    assert "error" in nama, f"run mati dalam diam — event: {nama}"
    assert nama[-1] == "end"
    (error,) = [e for e in events if e["event"] == "error"]
    assert "penalaran" in error["data"].lower()
    # Anggaran dinaikkan tepat sekali, bukan berulang sampai iterasi habis.
    assert len(completions.anggaran) == 2, (
        f"eskalasi harus sekali saja: {completions.anggaran}"
    )


@pytest.mark.anyio
async def test_argumen_tool_terpotong_dilaporkan_gagal():
    """Argumen terpotong tak boleh jadi "Menyisipkan 0 karakter" yang mengaku sukses."""
    terpotong = '{"markdown":"## 1.1 Latar Belakang\\n\\nIsi yang terpo'
    completions = _CompletionsPalsu([
        _giliran_tool("submit_plan", '{"tasks":["Tulis 1.1"]}'),
        _giliran_tool("doc_insert", terpotong, finish="length"),
    ])

    events = await _jalankan(completions)

    hasil = [e["data"] for e in events if e["event"] == "tool_result"]
    insert = [h for h in hasil if h["name"] == "doc_insert"]
    assert insert, "harus ada tool_result untuk doc_insert"
    assert all(h["ok"] is False for h in insert), (
        f"argumen terpotong harus dilaporkan gagal: {insert[:2]}"
    )
    assert all("terpotong" in h["summary"].lower() for h in insert)


@pytest.mark.anyio
async def test_doc_insert_markdown_kosong_ditolak():
    completions = _CompletionsPalsu([
        _giliran_tool("submit_plan", '{"tasks":["Tulis 1.1"]}'),
        _giliran_tool("doc_insert", '{"markdown":"   "}'),
    ])

    events = await _jalankan(completions)

    insert = [
        e["data"] for e in events
        if e["event"] == "tool_result" and e["data"]["name"] == "doc_insert"
    ]
    assert insert and all(h["ok"] is False for h in insert), (
        f"sisipan kosong harus ditolak: {insert[:2]}"
    )


@pytest.mark.anyio
async def test_anggaran_habis_di_tengah_pekerjaan_tidak_mengaku_tanpa_rencana():
    """Pesan error harus jujur: sebagian isi SUDAH ditulis, jadi jangan bilang
    "tanpa menghasilkan rencana kerja"."""
    completions = _CompletionsPalsu([
        _giliran_tool("submit_plan", '{"tasks":["Tulis 1.1","Tulis 1.2"]}'),
        _giliran_tool("doc_insert", '{"markdown":"## 1.1 Latar Belakang\\n\\nIsi."}'),
        _giliran_penalaran_habis(),
    ])

    events = await _jalankan(completions)

    (error,) = [e for e in events if e["event"] == "error"]
    assert "tanpa menghasilkan rencana" not in error["data"], (
        f"pesan menyesatkan — rencana sudah ada: {error['data']}"
    )
    assert "sebagian sudah ditulis" in error["data"].lower()


@pytest.mark.anyio
async def test_jalur_normal_tidak_terpengaruh():
    """Model yang sehat tetap berjalan tanpa eskalasi anggaran maupun error."""
    completions = _CompletionsPalsu([
        _giliran_tool("submit_plan", '{"tasks":["Tulis 1.1"]}'),
        _giliran_tool("set_task_status", '{"task_index":0,"status":"running"}'),
        _giliran_tool("doc_insert", '{"markdown":"## 1.1 Latar Belakang\\n\\nIsi lengkap."}'),
        _giliran_tool("set_task_status", '{"task_index":0,"status":"done"}'),
        _giliran_prosa("Selesai: sub-bagian 1.1 ditulis di akhir dokumen."),
    ])

    events = await _jalankan(completions)

    nama = _nama_event(events)
    assert "error" not in nama, f"jalur normal tidak boleh error: {events}"
    assert "plan" in nama and "text" in nama
    assert len(set(completions.anggaran)) == 1, (
        f"anggaran tak boleh naik pada model sehat: {completions.anggaran}"
    )
    insert = [
        e["data"] for e in events
        if e["event"] == "tool_result" and e["data"]["name"] == "doc_insert"
    ]
    assert insert and all(h["ok"] for h in insert)


@pytest.mark.anyio
async def test_tool_tulis_sebelum_rencana_ditolak_tanpa_menyentuh_dokumen():
    """`doc_insert` sebelum `submit_plan` tidak boleh sampai ke editor.

    `tool_choice="required"` memaksa model memanggil tool, tapi tidak bisa
    memaksa tool yang MANA. Model aktif proyek ini terukur langsung memanggil
    `doc_insert` dua kali dengan isi identik sebelum `submit_plan`, lalu sekali
    lagi sesudahnya — `find_in_document` menemukan 3 kecocokan, artinya paragraf
    yang sama tiga kali masuk dokumen pengguna. Tool tulis dieksekusi frontend
    (`fe: true`), jadi event yang lolos benar-benar mengubah dokumen.
    """
    completions = _CompletionsPalsu([
        _giliran_tool("doc_insert", '{"markdown":"Paragraf liar sebelum rencana."}'),
        _giliran_tool("submit_plan", '{"tasks":["Tulis 1.1"]}'),
        _giliran_tool("doc_insert", '{"markdown":"## 1.1 Latar Belakang\\n\\nIsi sah."}'),
        _giliran_prosa("Selesai."),
    ])

    events = await _jalankan(completions)

    ditulis = [
        e["data"] for e in events
        if e["event"] == "tool_call" and e["data"]["name"] == "doc_insert"
    ]
    assert len(ditulis) == 1, f"hanya tulisan setelah rencana yang boleh lolos: {ditulis}"
    assert "Isi sah" in ditulis[0]["args"]["markdown"]
    assert all("liar" not in str(e.get("data", "")) for e in events), events

    # Penolakan tidak boleh mematikan run: rencana tetap jalan sampai ringkasan.
    nama = _nama_event(events)
    assert "plan" in nama and "text" in nama
    assert "error" not in nama, events


@pytest.mark.anyio
async def test_prosa_tanpa_rencana_didorong_bukan_diterima_sebagai_ringkasan():
    """Model yang balik bertanya tanpa merencanakan harus didorong lanjut.

    Terukur lewat endpoint terhadap model aktif: model memanggil `read_document`
    dengan argumen asal, gagal, lalu menjawab "Bisakah Anda memberi tahu nama
    berkas?" — prosa itu dulu dipancarkan sebagai Ringkasan dan run berhenti
    tanpa satu baris ditulis, sehingga panel tampak "tidak terjadi apa-apa".
    Padahal isi dokumen sudah ada di konteks.
    """
    completions = _CompletionsPalsu([
        _giliran_prosa("Dokumen mana yang harus saya sunting?"),
        _giliran_tool("submit_plan", '{"tasks":["Tulis 1.1"]}'),
        _giliran_tool("doc_insert", '{"markdown":"## 1.1 Latar Belakang\\n\\nIsi nyata."}'),
        _giliran_prosa("Selesai: 1.1 ditulis."),
    ])

    events = await _jalankan(completions)

    nama = _nama_event(events)
    assert "plan" in nama, f"pertanyaan harus didorong jadi rencana: {events}"
    ringkasan = [e["data"] for e in events if e["event"] == "text"]
    assert ringkasan, events
    assert all("Dokumen mana" not in str(r) for r in ringkasan), (
        f"pertanyaan prematur tidak boleh bocor sebagai ringkasan: {ringkasan}"
    )
