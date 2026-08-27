"""Loop eksekusi agentic untuk Co-Writer ("Asisten Agentic" — vibe-writing).

Satu instruksi pengguna → AI menyusun RENCANA (daftar tugas) → mengeksekusi
tiap tugas dengan status real-time → mengoreksi diri → SATU ringkasan akhir.

Berbeda dari `agentic_writer.py` yang single-shot, modul ini adalah loop
tool-calling sungguhan (pola `agentic_chat.run_agentic_chat_stream`) dengan dua
kelas tool:

* **Tool tulis** (`doc_insert`, `doc_replace`, `cite_insert`) — TIDAK dieksekusi
  di server. Server memancarkan event `tool_call` dengan `fe: true`; frontend
  (Layer 2) yang menjalankannya lewat handle editor SuperDoc (Layer 0), lalu
  menyusun ringkasan akhir dari hasil NYATA. Server memperbarui buffer teks
  bayangan agar penalaran jangkar model tetap konsisten dengan tulisannya
  sendiri, dan mengembalikan sukses optimistik ke model.
* **Tool baca/kontrol** (`find_in_document`, `submit_plan`, `set_task_status`,
  serta tool riset opsional) — dieksekusi di server.

Alur ini membuat frontend tidak perlu komunikasi dua-arah di tengah SSE: server
memimpin loop, frontend mengeksekusi tulisan dan melaporkan kebenarannya di
ringkasan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agentic_chat import _parse_dsml_tool_calls, _strip_dsml, _DSML_START
from app.services.citation_tools import (
    SitasiError,
    bibliografi_markdown,
    cite_add,
    cite_list,
    ref_read,
)
from app.services.document_tools import (
    read_document,
    search_in_document,
    search_web,
    fetch_webpage,
    arxiv_search,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Anggaran token keluaran per iterasi.
#
# Berbeda dari `research_chat.output_token_budget()` yang menganggarkan satu
# balasan chat pendek ("Maksimal 220 kata" → 2.700 token termasuk cadangan
# penalaran), loop ini menulis potongan dokumen sebagai argumen JSON tool.
# Anggaran yang terlalu kecil membuat argumen `doc_insert` terpotong dan isinya
# hilang, jadi angka di sini disengaja lebih besar.
#
# `_CADANGAN_PENALARAN` ditambahkan DI ATAS anggaran isi, bukan diambil darinya:
# model aktif proyek ini mendaftarkan diri sebagai ["text"]/["text","tools"]
# namun tetap mengirim jejak penalaran yang memakan `max_tokens` (lihat
# tests/test_agentic_writer_penalaran.py). Tanpa cadangan itu, giliran pertama
# berakhir `finish_reason="length"` tanpa satu tool call.
# --------------------------------------------------------------------------- #
_ANGGARAN_ISI = 4000
_CADANGAN_PENALARAN = 1200
# Batas atas saat percobaan ulang. Lebih tinggi dari
# `agentic_writer._ANGGARAN_ULANG_MAKS` (6000) yang hanya perlu memuat satu
# jawaban chat; di sini satu giliran harus memuat jejak penalaran DAN argumen
# tool berisi satu sub-bagian dokumen.
_ANGGARAN_ULANG_MAKS = 12000


def _anggaran_awal(context_window: int) -> int:
    """Anggaran keluaran satu iterasi, dibatasi jendela konteks model."""
    budget = _ANGGARAN_ISI + _CADANGAN_PENALARAN
    context_cap = max(1500, int(context_window or 8000) // 4)
    return max(1500, min(budget, context_cap))


def _anggaran_naik(max_tokens: int, context_window: int) -> int:
    """Anggaran percobaan ulang saat penalaran menghabiskan anggaran.

    Rumus mengikuti `agentic_writer.ask()` (kali tiga, dengan lantai), hanya
    dengan langit-langit yang lebih tinggi untuk memuat argumen tool.
    """
    context_cap = max(1500, int(context_window or 8000) // 4)
    naik = min(_ANGGARAN_ULANG_MAKS, max(max_tokens * 3, 2500))
    return min(naik, context_cap)


# --------------------------------------------------------------------------- #
# Mode eksekusi (default "seimbang"). Menyetel kedalaman loop, suhu, dan apakah
# riset web otonom diaktifkan. Tetap menghormati capability_tier di endpoint.
#
# Batas iterasi dihitung dari pekerjaan nyata "satu bab utuh": tiap sub-bab butuh
# ~4 panggilan (set_task_status → find_in_document → doc_insert → set_task_status),
# dan bab dengan 6 sub-bab yang memuat sitasi menambah ~2 panggilan riset per
# sub-bab (search_web/arxiv_search → cite_add). Jadi satu bab bersitasi memerlukan
# ~40 iterasi; "seimbang" yang dulu 24 selalu kehabisan iterasi di tengah bab dan
# berhenti dengan tugas masih 'pending'.
# --------------------------------------------------------------------------- #
_MODES: dict[str, dict[str, Any]] = {
    # "cepat" untuk suntingan setempat: web dimatikan supaya tidak ada jeda riset.
    # Konsekuensinya `cite_add` tak punya sumber terverifikasi, jadi mode ini
    # memang bukan untuk menulis bagian bersitasi.
    "cepat": {"max_iterations": 16, "temperature": 0.3, "web": False},
    "seimbang": {"max_iterations": 44, "temperature": 0.4, "web": True},
    "menyeluruh": {"max_iterations": 72, "temperature": 0.5, "web": True},
}
_DEFAULT_MODE = "seimbang"


def _mode_config(mode: str | None) -> dict[str, Any]:
    return _MODES.get((mode or _DEFAULT_MODE).lower(), _MODES[_DEFAULT_MODE])


# --------------------------------------------------------------------------- #
# Buffer teks bayangan — cermin ringan dokumen supaya find_in_document dan
# validasi jangkar melihat tulisan model sendiri dalam satu giliran. BUKAN
# sumber kebenaran; editor SuperDoc di frontend yang otoritatif.
# --------------------------------------------------------------------------- #
def _tolerant_replace(text: str, find: str, replace: str, all_occurrences: bool) -> tuple[str, int]:
    """Ganti `find`→`replace` dengan TOLERANSI SPASI.

    Model gratis kerap mengetik ulang `find` dari ingatan, sehingga spasi/ganti
    baris beda tipis dari naskah → substring persis gagal → perbaikan hilang
    diam-diam. (1) coba substring persis (paling aman); (2) bila gagal, cocokkan
    lewat regex di mana tiap deret whitespace pada `find` boleh cocok dengan
    whitespace apa pun. Kembalikan (teks_baru, jumlah_ganti).
    """
    if not find:
        return text, 0
    if find in text:
        n = text.count(find) if all_occurrences else 1
        return text.replace(find, replace, -1 if all_occurrences else 1), n
    tokens = [t for t in re.split(r"\s+", find.strip()) if t]
    if not tokens:
        return text, 0
    try:
        rx = re.compile(r"\s+".join(re.escape(t) for t in tokens))
    except re.error:
        return text, 0
    # Lambda pengganti: `replace` tak boleh diinterpretasi sebagai template \g<>.
    return rx.subn(lambda _m: replace, text, count=0 if all_occurrences else 1)


class _ShadowDoc:
    def __init__(self, text: str) -> None:
        # Simpan sebagai daftar baris agar sisip berjangkar mudah & murah.
        self._lines: list[str] = (text or "").splitlines()

    def to_text(self) -> str:
        return "\n".join(self._lines)

    def _find_line(self, anchor: str, case_sensitive: bool) -> int:
        if not anchor:
            return -1
        needle = anchor if case_sensitive else anchor.lower()
        for i, line in enumerate(self._lines):
            hay = line if case_sensitive else line.lower()
            if needle in hay:
                return i
        return -1

    def insert(self, markdown: str, anchor: str | None, placement: str) -> None:
        block = (markdown or "").splitlines() or [markdown or ""]
        if not anchor:
            if self._lines:
                self._lines.append("")
            self._lines.extend(block)
            return
        idx = self._find_line(anchor, case_sensitive=False)
        if idx < 0:
            # Jangkar tak ada di bayangan — tetap tambahkan di akhir supaya isinya
            # tak hilang; frontend menangani penempatan sebenarnya.
            self._lines.extend([""] + block)
            return
        at = idx + 1 if placement in ("after", "insideEnd") else idx
        self._lines[at:at] = block

    def replace(self, find: str, replace: str, all_occurrences: bool) -> int:
        if not find:
            return 0
        new_text, n = _tolerant_replace(
            "\n".join(self._lines), find, replace, all_occurrences
        )
        if n:
            self._lines = new_text.splitlines()
        return n

    def find(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not query:
            return []
        needle = query.lower()
        hits: list[dict[str, Any]] = []
        for i, line in enumerate(self._lines):
            if needle in line.lower():
                start = max(0, i - 1)
                end = min(len(self._lines), i + 2)
                hits.append({
                    "line": i + 1,
                    "snippet": "\n".join(self._lines[start:end]).strip()[:400],
                })
                if len(hits) >= limit:
                    break
        return hits


# --------------------------------------------------------------------------- #
# Skema tool. Tool tulis ditandai lewat _FE_TOOLS agar endpoint memancarkan
# `fe: true`. Tool kontrol (submit_plan/set_task_status) tak pernah sampai ke
# editor. Tool riset ditambahkan bersyarat sesuai mode/tier.
# --------------------------------------------------------------------------- #
_FE_TOOLS = {"doc_insert", "doc_replace", "cite_insert"}
_CONTROL_TOOLS = {"submit_plan", "set_task_status"}

_TOOL_SUBMIT_PLAN = {
    "type": "function",
    "function": {
        "name": "submit_plan",
        "description": (
            "WAJIB dipanggil PERTAMA sebelum menulis apa pun. Kirim rencana kerja "
            "sebagai daftar tugas singkat dan konkret (urut eksekusi)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {"type": "string", "description": "Judul satu tugas, ringkas & actionable."},
                    "description": "Daftar tugas urut eksekusi (2-8 tugas ideal).",
                }
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
    },
}

_TOOL_SET_TASK_STATUS = {
    "type": "function",
    "function": {
        "name": "set_task_status",
        "description": (
            "Perbarui status satu tugas rencana. Panggil 'running' saat mulai "
            "mengerjakan sebuah tugas, dan 'done' setelah selesai (atau 'failed' "
            "bila tak bisa diselesaikan)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_index": {"type": "integer", "description": "Indeks tugas (0-based) dari rencana."},
                "status": {"type": "string", "enum": ["running", "done", "failed"]},
                "note": {"type": "string", "description": "Catatan singkat opsional."},
            },
            "required": ["task_index", "status"],
            "additionalProperties": False,
        },
    },
}

_TOOL_DOC_INSERT = {
    "type": "function",
    "function": {
        "name": "doc_insert",
        "description": (
            "Sisipkan teks (markdown) ke dokumen. Tanpa anchor_text, ditambahkan "
            "di akhir dokumen. Dengan anchor_text, disisipkan relatif terhadap "
            "paragraf yang memuat teks jangkar itu."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "markdown": {"type": "string", "description": "Konten markdown yang disisipkan."},
                "anchor_text": {"type": "string", "description": "Teks unik penanda lokasi (opsional)."},
                "placement": {
                    "type": "string",
                    "enum": ["before", "after"],
                    "description": "Sisip sebelum/sesudah paragraf jangkar (default after).",
                },
            },
            "required": ["markdown"],
            "additionalProperties": False,
        },
    },
}

_TOOL_DOC_REPLACE = {
    "type": "function",
    "function": {
        "name": "doc_replace",
        "description": (
            "Ganti kemunculan teks `find` dengan `replace`. Untuk merapikan, "
            "memperbaiki, atau menyunting kalimat yang SUDAH ada di dokumen. "
            "PENTING: `find` harus potongan PERSIS & PENDEK (idealnya satu "
            "kalimat/frasa, ≲120 karakter) yang kamu SALIN VERBATIM dari hasil "
            "`find_in_document` — jangan diketik ulang dari ingatan, jangan "
            "diparafrasa, jangan menyertakan seluruh paragraf. Pencocokan kini "
            "toleran beda spasi/ganti baris, tapi kata-katanya tetap harus sama."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "find": {"type": "string", "description": "Potongan PERSIS & PENDEK yang disalin dari find_in_document (bukan parafrasa)."},
                "replace": {"type": "string", "description": "Teks pengganti (versi rapi/perbaikannya)."},
                "all": {"type": "boolean", "description": "Ganti semua kemunculan (default hanya pertama)."},
            },
            "required": ["find", "replace"],
            "additionalProperties": False,
        },
    },
}

_TOOL_CITE_INSERT = {
    "type": "function",
    "function": {
        "name": "cite_insert",
        "description": (
            "Sisipkan SITASI Word hidup (field OOXML) pada lokasi jangkar. "
            "JANGAN mengarang sumber: pakai hanya metadata sumber yang benar-benar "
            "diverifikasi (mis. dari find/hasil riset). Sitasi disisipkan setelah "
            "teks jangkar."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "anchor_text": {"type": "string", "description": "Teks penanda lokasi sitasi."},
                "title": {"type": "string", "description": "Judul sumber."},
                "authors": {
                    "type": "array",
                    "items": {"type": "string", "description": "Nama penulis 'Depan Belakang'."},
                    "description": "Daftar penulis.",
                },
                "year": {"type": "integer", "description": "Tahun terbit."},
                "doi": {"type": "string", "description": "DOI (opsional tapi disarankan)."},
                "journal": {"type": "string", "description": "Nama jurnal/penerbit (opsional)."},
                "source_type": {
                    "type": "string",
                    "description": "Jenis sumber (journalArticle/book/website/…). Default journalArticle.",
                },
            },
            "required": ["anchor_text", "title", "authors"],
            "additionalProperties": False,
        },
    },
}

_TOOL_CITE_ADD = {
    "type": "function",
    "function": {
        "name": "cite_add",
        "description": (
            "Simpan SATU sumber terverifikasi ke perpustakaan referensi dan dapatkan "
            "nomor sitasinya. Server yang menentukan nomornya lalu mengembalikannya "
            "(mis. {\"sitasi\": \"[3]\"}); tulis nomor ITU apa adanya di teks. "
            "JANGAN pernah mengarang nomor sitasi sendiri. Pakai hanya metadata dari "
            "hasil search_web/arxiv_search/fetch_webpage — jangan mengarang judul, "
            "penulis, atau DOI. Sumber yang sama (DOI/judul) mengembalikan nomor "
            "yang sudah ada, jadi aman dipanggil ulang. Nomor inilah yang membuat "
            "Daftar Pustaka terisi otomatis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Judul sumber (wajib)."},
                "authors": {
                    "type": "array",
                    "items": {"type": "string", "description": "Nama penulis 'Depan Belakang'."},
                    "description": "Daftar penulis (wajib, minimal satu).",
                },
                "year": {"type": "integer", "description": "Tahun terbit."},
                "journal": {"type": "string", "description": "Nama jurnal/konferensi/penerbit."},
                "doi": {"type": "string", "description": "DOI bila ada."},
                "url": {"type": "string", "description": "URL sumber bila ada."},
            },
            "required": ["title", "authors"],
            "additionalProperties": False,
        },
    },
}

_TOOL_CITE_LIST = {
    "type": "function",
    "function": {
        "name": "cite_list",
        "description": (
            "Lihat sumber yang sudah tersimpan beserta nomor sitasinya. Panggil ini "
            "sebelum menulis bagian berisi rujukan supaya klaim yang memakai sumber "
            "sama memakai nomor yang sama, dan supaya kamu tahu nomor yang sah."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

_TOOL_REF_READ = {
    "type": "function",
    "function": {
        "name": "ref_read",
        "description": (
            "Baca ISI satu sumber di perpustakaan referensi pengguna (jurnal yang "
            "diunggah sendiri maupun sumber hasil riset). Pakai ini untuk menulis "
            "tinjauan pustaka yang benar-benar bersumber dari bacaan, bukan dari "
            "ingatan. Argumen `reference` boleh nomor sitasi ('3' atau '[3]'), "
            "judul, atau id. Untuk sumber daring, tool akan memberi URL-nya supaya "
            "kamu lanjut dengan fetch_webpage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "Nomor sitasi, judul, atau id sumber.",
                },
                "max_chars": {"type": "integer", "description": "Batas panjang kutipan isi."},
            },
            "required": ["reference"],
            "additionalProperties": False,
        },
    },
}

_TOOL_BIBLIOGRAPHY = {
    "type": "function",
    "function": {
        "name": "cite_bibliography",
        "description": (
            "WAJIB dipanggil sebagai tugas TERAKHIR bila naskah memuat sitasi [n]. "
            "Server memindai sitasi yang benar-benar kamu tulis lalu mengembalikan "
            "blok DAFTAR PUSTAKA yang sudah diformat (IEEE). Sisipkan hasilnya "
            "APA ADANYA dengan doc_insert di akhir dokumen — jangan menyusun "
            "daftar pustaka sendiri dan jangan mengubah teksnya."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

_TOOL_FIND_IN_DOC = {
    "type": "function",
    "function": {
        "name": "find_in_document",
        "description": (
            "Cari teks di dokumen yang sedang disunting untuk menemukan lokasi "
            "jangkar SEBELUM doc_replace/cite_insert, atau untuk memeriksa apa "
            "yang sudah kamu tulis. Mengembalikan cuplikan + nomor baris."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Teks yang dicari."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def _build_tools(mode_cfg: dict[str, Any], allow_web: bool) -> list[dict]:
    tools = [
        _TOOL_SUBMIT_PLAN,
        _TOOL_SET_TASK_STATUS,
        _TOOL_DOC_INSERT,
        _TOOL_DOC_REPLACE,
        _TOOL_CITE_INSERT,
        _TOOL_CITE_ADD,
        _TOOL_CITE_LIST,
        _TOOL_REF_READ,
        _TOOL_BIBLIOGRAPHY,
        _TOOL_FIND_IN_DOC,
        # Baca dokumen milik user (bukan dokumen aktif) — konteks tambahan.
        {
            "type": "function",
            "function": {
                "name": "read_document",
                "description": "Baca isi dokumen milik user (referensi/lampiran) sebagai konteks.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id_or_filename": {"type": "string"},
                        "max_chars": {"type": "integer"},
                    },
                    "required": ["document_id_or_filename"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_in_document",
                "description": "Cari kata kunci di dokumen referensi milik user.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "document_id_or_filename": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "required": ["document_id_or_filename", "query"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    if allow_web and mode_cfg.get("web"):
        tools.extend([
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "description": "Cari informasi/rujukan di internet (real-time).",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "arxiv_search",
                    "description": "Cari paper akademik di arXiv.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_webpage",
                    "description": "Baca isi lengkap satu URL hasil pencarian.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                },
            },
        ])
    return tools


def _muat_kraf_skill() -> str:
    """Muat badan SKILL.md 'penulisan-jurnal-akademik' (tanpa frontmatter) untuk
    disuntik sebagai lapis kraf ke prompt sistem. Defensif: bila berkas tak ada,
    kembalikan string kosong supaya agent tetap jalan dengan kontrak operasional.
    """
    try:
        p = Path(__file__).parent / "skills" / "penulisan-jurnal-akademik" / "SKILL.md"
        teks = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    # Buang frontmatter YAML (--- ... ---) di awal; sisakan badan instruksi.
    if teks.startswith("---"):
        bagian = teks.split("---", 2)
        if len(bagian) == 3:
            teks = bagian[2]
    return teks.strip()


_SKILL_KRAF = _muat_kraf_skill()


_SYSTEM_PROMPT = (
    "Kamu adalah ASISTEN AGENTIC penulis laporan akademik di dalam editor mirip "
    "Word. Kamu tidak sekadar menjawab — kamu BEKERJA: menyusun rencana lalu "
    "mengeksekusinya langsung ke dokumen pengguna lewat tool.\n\n"
    "ALUR WAJIB:\n"
    "0. GROUNDING DULU (sebelum merencana): panggil `cite_list` untuk melihat "
    "referensi yang tersedia — utamakan jurnal yang diunggah pengguna — dan untuk "
    "bagian yang bergantung isi sumber, `ref_read` sumber kuncinya. Ini BUKAN "
    "menulis; ini mengenali bahan agar rencanamu berpijak pada sumber nyata.\n"
    "1. PANGGIL `submit_plan` dengan daftar tugas konkret sesuai instruksi "
    "pengguna DAN sumber yang ada. Rencana WAJIB memuat, menjelang akhir, satu "
    "tugas 'Penyempurnaan & uji-baca' tepat sebelum tugas Daftar Pustaka. Jangan "
    "MENULIS (doc_insert/doc_replace/cite_insert) apa pun sebelum submit_plan.\n"
    "2. Kerjakan tugas SATU per SATU sesuai urutan rencana. Untuk tiap tugas: "
    "panggil `set_task_status(index, 'running')`, lakukan pekerjaannya (tool "
    "tulis/riset), lalu `set_task_status(index, 'done')`. WAJIB menandai setiap "
    "tugas — jangan menulis isi tanpa memperbarui status tugasnya.\n"
    "3. Gunakan `doc_insert` untuk menambah isi, `doc_replace` untuk menyunting "
    "teks yang SUDAH ada, `cite_insert` untuk sitasi. TULIS SEKALI JADI: tiap "
    "`doc_insert` harus SUDAH berkualitas final — kalimat mengalir, ada transisi "
    "antar-paragraf, klaim penting bersitasi. JANGAN menulis draf kasar dengan "
    "niat 'nanti dirapikan lewat doc_replace'; langkah rapikan itu rapuh dan bisa "
    "gagal, jadi kerapihan HARUS lahir sejak insert pertama.\n"
    "4. Sebelum `doc_replace` atau `cite_insert`, WAJIB `find_in_document` dulu, "
    "lalu SALIN VERBATIM potongan PENDEK (satu frasa/kalimat) dari hasilnya "
    "sebagai `find` — jangan mengetik `find` dari ingatan, jangan sepanjang "
    "paragraf. Kalau `doc_replace` menjawab 'tak ditemukan', JANGAN mengarang "
    "atau mengulang membabi buta: `find_in_document` lagi untuk menyalin teks "
    "yang benar, atau lewati suntingan itu.\n"
    "5. PENYEMPURNAAN & UJI-BACA (tugas kedua-terakhir, sebelum Daftar Pustaka): "
    "baca ulang lewat `find_in_document`, lalu lakukan HANYA suntingan kecil & "
    "bedah (surgical) via `doc_replace` dengan `find` yang disalin verbatim — "
    "buang klise, padatkan kalimat bertele-tele, pastikan tiap klaim penting "
    "bersitasi, dan periksa tak ada kontradiksi antar-bagian. JANGAN menulis "
    "ulang paragraf besar sekaligus (pasti gagal). Karena kamu sudah menulis "
    "berkualitas sejak awal (poin 3), tahap ini ringan. Rincian di bagian SKILL "
    "di bawah. Jangan tandai tugas ini 'done' sebelum benar-benar membaca ulang "
    "isinya.\n"
    "6. Setelah SEMUA tugas berstatus 'done', berikan RINGKASAN akhir singkat "
    "dalam bahasa Indonesia (apa yang dikerjakan, di mana) TANPA memanggil tool "
    "lagi. JANGAN memberi ringkasan selama masih ada tugas 'pending'/'running'.\n\n"
    "JANGAN BERHENTI DI TENGAH:\n"
    "- Selama masih ADA tugas yang belum 'done', kamu WAJIB terus memanggil tool "
    "untuk mengerjakan tugas berikutnya. Dilarang berhenti, bertanya, atau menulis "
    "prosa 'sudah saya buatkan sebagian…' sebelum seluruh rencana tuntas.\n"
    "- Tulis dokumen panjang PER SUB-BAGIAN dalam beberapa panggilan `doc_insert` "
    "terpisah (mis. tiap sub-bab 1.1, 1.2 satu panggilan), JANGAN satu bab raksasa "
    "dalam satu panggilan — argumen tool yang terlalu panjang akan terpotong dan "
    "isinya hilang. Lebih baik banyak insert kecil yang tuntas.\n\n"
    "PENEMPATAN (PENTING — hindari 'nempel di bawah'):\n"
    "- `doc_insert` TANPA `anchor_text` selalu menaruh teks di AKHIR dokumen. "
    "Pakai ini HANYA untuk menambah bagian yang benar-benar baru di ujung draf.\n"
    "- Untuk menyisipkan/menyunting di tempat tertentu, SELALU `find_in_document` "
    "dulu untuk menemukan kalimat penanda, lalu `doc_insert` dengan `anchor_text` "
    "(+ placement before/after) atau `doc_replace`. Kalau pengguna minta "
    "memperbaiki/mengganti bagian yang SUDAH ada, pakai `doc_replace` — JANGAN "
    "menambah salinan baru di bawah.\n\n"
    "ATURAN ISI:\n"
    "- Tulis dalam Bahasa Indonesia akademik. Isi tool `doc_insert`/`doc_replace` "
    "berupa MARKDOWN MURNI: judul jurnal pakai `# Judul` (SATU tanda pagar → "
    "otomatis besar, tebal, & rata tengah di editor), sub-bab `##`/`###`, tebal "
    "`**...**`, daftar `-`, dan pisahkan antar-paragraf dengan BARIS KOSONG.\n"
    "- DILARANG KERAS mengeluarkan LaTeX. JANGAN pernah menulis `\\title{}`, "
    "`\\maketitle`, `\\author{}`, `\\date{}`, `\\begin{...}`/`\\end{...}`, "
    "`\\section{}`, atau `\\textbf{}` — WALAUPUN 'POTRET DOKUMEN' di bawah kebetulan "
    "berisi LaTeX. Ketebalan & rata-tengah judul dibuat oleh editor dari `#`, "
    "BUKAN dari perintah LaTeX. Kalau potret berisi LaTeX, abaikan sintaksnya dan "
    "tetap tulis Markdown.\n"
    "- DILARANG mengarang fakta, angka, DOI, atau referensi. Untuk data yang belum "
    "diberikan pengguna, tulis penanda `[BUTUH DATA: ...]`.\n"
    "- Rapikan hanya yang perlu; jangan menghapus isi pengguna tanpa alasan.\n"
    "- Bila konteks memuat 'TEMPLATE/CONTOH WAJIB DIIKUTI', susunan bab, "
    "penomoran, urutan bagian, dan gaya tulisanmu WAJIB mengikuti template itu "
    "PERSIS — jangan mengarang struktur sendiri, jangan menambah/menghapus bab di "
    "luar kerangka template.\n"
    "- Efisien: jangan mengulang tool yang sama tanpa hasil baru.\n\n"
    "SITASI (WAJIB diikuti — ini yang mengisi Daftar Pustaka):\n"
    "Laporan ini memakai sitasi bernomor gaya IEEE: `[1]`, `[2]`, dan seterusnya. "
    "Nomornya TIDAK boleh kamu tentukan sendiri — SERVER yang menentukan.\n"
    "1. Untuk klaim yang butuh rujukan, cari sumbernya dulu (`search_web` / "
    "`arxiv_search`, perdalam dengan `fetch_webpage` bila perlu).\n"
    "2. Panggil `cite_add` dengan metadata sumber itu (judul, penulis, tahun, DOI "
    "bila ada). Server membalas nomornya, mis. `{\"sitasi\": \"[3]\"}`.\n"
    "3. Tulis nomor ITU apa adanya di teks, tepat setelah klaimnya — mis. "
    "\"...meningkatkan capaian belajar [3].\" JANGAN menulis nomor yang tidak "
    "pernah dikembalikan `cite_add`, dan JANGAN menulis `[SITASI: ...]`.\n"
    "4. Panggil `cite_list` lebih dulu bila ragu; sumber yang sudah ada "
    "mengembalikan nomor lamanya, sehingga rujukan yang sama tetap satu nomor. "
    "`cite_list` juga memperlihatkan jurnal yang SUDAH diunggah pengguna — "
    "utamakan sumber itu, dan baca isinya dengan `ref_read` (boleh pakai nomor "
    "sitasinya) sebelum menulis tinjauan pustaka, supaya isinya benar-benar "
    "bersumber dari bacaan dan bukan dari ingatan.\n"
    "5. Kalau sumber tepercaya tidak ditemukan, JANGAN mengarang: tulis kalimatnya "
    "tanpa nomor sitasi dan sebutkan kekurangan itu di ringkasan akhir.\n"
    "6. TUGAS TERAKHIR sebelum ringkasan, bila naskah memuat sitasi `[n]`: panggil "
    "`cite_bibliography`, lalu sisipkan nilai `markdown` yang dikembalikannya APA "
    "ADANYA lewat `doc_insert` tanpa `anchor_text`. Itulah DAFTAR PUSTAKA-nya — "
    "jangan menyusunnya sendiri dan jangan mengarang entri.\n"
    "Daftar Pustaka disusun dari nomor yang benar-benar kamu tulis, jadi nomor yang "
    "tidak dipakai di teks tidak akan muncul di sana."
)

# Sisipkan badan SKILL 'penulisan-jurnal-akademik' sebagai lapis kraf di atas
# kontrak operasional (pola progressive-disclosure Agent Skills): kontrak inti di
# atas selalu ada; playbook mutu di-load dari berkas yang bisa disetel terpisah.
if _SKILL_KRAF:
    _SYSTEM_PROMPT += (
        "\n\n===== SKILL: PENULISAN JURNAL AKADEMIK (playbook mutu) =====\n"
        + _SKILL_KRAF
    )


# Prompt untuk FASE PROPOSE (brainstorm "rencana → setujui → kerjakan"). Di fase
# ini agent BELUM menyentuh dokumen: ia grounding, mengusulkan rencana, menulis
# brainstorm singkat, lalu BERHENTI menunggu persetujuan. Kecerdasan ada di alur
# (grounding + berpikir di depan pengguna + gerbang persetujuan), sehingga tetap
# terasa pintar walau modelnya lemah. Eksekusi terjadi di giliran terpisah
# (fase execute) setelah pengguna menyetujui.
_PROPOSE_SYSTEM_PROMPT = (
    "Kamu adalah ASISTEN AGENTIC penulis laporan akademik di dalam editor mirip "
    "Word. SAAT INI kamu di TAHAP USUL RENCANA (brainstorm) — kamu BELUM menulis "
    "ke dokumen. Tujuanmu: mengusulkan rencana yang berpijak pada sumber nyata, "
    "lalu BERHENTI menunggu persetujuan pengguna.\n\n"
    "LAKUKAN BERURUTAN:\n"
    "1. GROUNDING DULU: panggil `cite_list` untuk melihat referensi & catatan "
    "yang tersedia — UTAMAKAN jurnal/catatan yang diunggah pengguna (termasuk "
    "catatan dari vault Obsidian bila ada). Untuk bagian yang bergantung isi "
    "sumber, baca sumber kuncinya dengan `ref_read`. Ini mengenali bahan supaya "
    "rencanamu berpijak pada sumber, bukan mengarang. Boleh `search_web`/"
    "`arxiv_search` bila perlu, TAPI utamakan bahan pengguna lebih dulu.\n"
    "2. `submit_plan` dengan daftar tugas konkret sesuai instruksi pengguna DAN "
    "sumber yang ada (struktur IMRaD bila itu laporan penelitian). Bila konteks "
    "memuat 'TEMPLATE/CONTOH WAJIB DIIKUTI', daftar tugas & urutan/penomoran bab "
    "dalam rencana WAJIB mengikuti struktur template itu PERSIS. Sertakan, "
    "menjelang akhir, tugas 'Penyempurnaan & uji-baca' tepat sebelum tugas "
    "'Daftar Pustaka'.\n"
    "3. Setelah `submit_plan`, TULIS BRAINSTORM SINGKAT dalam Bahasa Indonesia "
    "(2–5 kalimat, prosa biasa — BUKAN tool): jelaskan kenapa struktur itu, "
    "sumber/catatan mana yang akan dipakai (sebut nomor sitasi `[n]` atau nama "
    "catatan), dan asumsi penting. Tutup dengan ajakan minta persetujuan, mis. "
    "'Setuju saya kerjakan?'.\n"
    "4. BERHENTI. Di fase ini DILARANG KERAS memanggil `doc_insert`, "
    "`doc_replace`, `cite_insert`, atau `set_task_status`. JANGAN mengeksekusi "
    "rencana. Dokumen TIDAK boleh berubah sedikit pun. Eksekusi baru terjadi "
    "SETELAH pengguna menyetujui, di giliran berikutnya.\n\n"
    "Hasil akhirmu adalah RENCANA + brainstorm singkat, BUKAN dokumen yang "
    "berubah. Jangan mengarang fakta/DOI/referensi; kalau sumber kurang, katakan "
    "di brainstorm."
)


async def run_agent_stream(
    client: AsyncOpenAI,
    model_name: str,
    *,
    instruction: str,
    doc_context: str,
    db: AsyncSession,
    user_id: uuid.UUID,
    mode: str = _DEFAULT_MODE,
    allow_web: bool = True,
    max_tokens: int | None = None,
    context_window: int = 8000,
    selection_text: str | None = None,
    extra_context: str | None = None,
    phase: str = "execute",
    approved_tasks: list[str] | None = None,
    template_context: str | None = None,
    session_memory: str | None = None,
) -> AsyncGenerator[str, None]:
    """Jalankan loop plan→execute→summarize; yield event NDJSON (dibungkus SSE
    di endpoint).

    Event yang dipancarkan (field `event`):
      plan {tasks:[{index,title,status}]}
      task_status {index,status,note?}
      tool_call {id,name,args,fe:bool}
      tool_result {id,name,ok,summary}
      text {delta}            — ringkasan akhir yang mengalir
      reasoning {delta}
      usage {...}
      error {detail}
      end {}
    """
    mode_cfg = _mode_config(mode)
    tools = _build_tools(mode_cfg, allow_web)
    shadow = _ShadowDoc(doc_context or "")

    # Cadangan penalaran diberikan tanpa memeriksa daftar kemampuan — lihat
    # catatan di _ANGGARAN_ISI.
    if max_tokens is None:
        max_tokens = _anggaran_awal(context_window)

    context_note = ""
    if doc_context:
        # Beri model potret ringkas dokumen (dipotong): cukup untuk merencanakan &
        # mencari jangkar, tanpa membanjiri jendela konteks model murah.
        snippet = doc_context.strip()
        if len(snippet) > 6000:
            snippet = snippet[:3000] + "\n\n[...dipotong...]\n\n" + snippet[-2000:]
        context_note = f"\n\nPOTRET DOKUMEN SAAT INI (ringkas):\n{snippet}"
        # Sebagian dokumen lama tersimpan sebagai LaTeX. Kalau potret berisi
        # sintaks itu, model murah cenderung MENIRUNYA (menulis \title{}, \maketitle,
        # dst.) meski aturan isi minta Markdown. Tegaskan ulang di sini, dekat
        # konteksnya, supaya larangan tak "tenggelam" oleh potret yang panjang.
        if re.search(r"\\(?:title|maketitle|author|date|section|begin|end|textbf|documentclass)\b", snippet):
            context_note += (
                "\n\n(CATATAN FORMAT PENTING: potret di atas kebetulan tersimpan dalam "
                "sintaks LaTeX. ABAIKAN — JANGAN menirunya. Semua teks yang kamu tulis "
                "lewat doc_insert/doc_replace WAJIB Markdown murni: judul `# ...`, "
                "tebal `**...**`. DILARANG mengeluarkan `\\title`, `\\maketitle`, "
                "`\\begin{...}`, `\\section`, atau `\\textbf{...}`.)"
            )
    if selection_text:
        context_note += f"\n\nTEKS YANG SEDANG DISOROT PENGGUNA:\n{selection_text.strip()[:2000]}"
    if extra_context:
        context_note += f"\n\nKONTEKS TAMBAHAN DARI PENGGUNA (impor chat / lampiran):\n{extra_context.strip()[:6000]}"
    if template_context:
        # Template/contoh dari pengguna = KERANGKA WAJIB, bukan sekadar konteks
        # longgar. Diperkuat lagi di kedua prompt sistem (propose & execute).
        context_note += (
            "\n\nTEMPLATE/CONTOH WAJIB DIIKUTI (ikuti struktur, urutan & "
            "penomoran bab, gaya penulisan, dan format PERSIS seperti kerangka "
            "ini — JANGAN menyusun struktur sendiri di luar template):\n"
            f"{template_context.strip()[:6000]}"
        )
    if session_memory:
        # Kontinuitas antar-run di dokumen yang sama (desain tetap stateless:
        # memori disuntik sebagai teks, tidak disimpan riwayat di BE). Di Mode
        # Word, doc_context hanya berisi outline heading — memori inilah yang
        # mencegah agent menduplikasi bagian yang sudah ditulis run sebelumnya.
        context_note += (
            "\n\nRINGKASAN SESI SEBELUMNYA DI DOKUMEN INI (lanjutkan dari sini; "
            "JANGAN mengulang tugas yang sudah 'selesai', JANGAN menduplikasi "
            "bagian yang sudah ada di dokumen — fokuskan ke yang belum tuntas):\n"
            f"{session_memory.strip()[:3000]}"
        )

    # Pilih kontrak sesuai fase: propose (brainstorm, read-only) vs execute
    # (perilaku lama: rencanakan bila perlu lalu tulis sampai tuntas).
    system_prompt = _PROPOSE_SYSTEM_PROMPT if phase == "propose" else _SYSTEM_PROMPT

    # Fase execute dgn rencana yang SUDAH disetujui: sisipkan daftar tugas ke
    # instruksi supaya model tidak merencana ulang, langsung mengeksekusi.
    approved_note = ""
    if phase == "execute" and approved_tasks:
        _daftar = "\n".join(f"{i}. {t}" for i, t in enumerate(approved_tasks))
        approved_note = (
            "\n\nRENCANA SUDAH DISETUJUI PENGGUNA. JANGAN panggil `submit_plan` "
            "lagi. Langsung KERJAKAN tugas di bawah SATU per SATU (untuk tiap "
            "tugas: `set_task_status` 'running' → tulis dgn doc_insert/doc_replace "
            f"→ 'done'):\n{_daftar}"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"INSTRUKSI: {instruction}{context_note}{approved_note}"},
    ]

    plan_tasks: list[dict[str, Any]] = []
    plan_submitted = False

    # Fase execute: pra-isi daftar tugas dari rencana yang disetujui (bentuk
    # PERSIS seperti cabang submit_plan) & tandai plan_submitted=True SEBELUM loop.
    # Efeknya: (a) tool_choice langsung "auto" (tak dipaksa submit_plan lagi) dan
    # (b) tool tulis tidak diblokir Guard D. Event `plan` normalnya HANYA keluar
    # di cabang submit_plan, jadi pancarkan MANUAL di sini agar FE tetap
    # menampilkan daftar tugas untuk melacak `task_status`.
    if phase == "execute" and approved_tasks:
        for i, title in enumerate(approved_tasks):
            _t = title if isinstance(title, str) else str(title)
            plan_tasks.append({"index": i, "title": _t.strip()[:200], "status": "pending"})
        plan_submitted = True
        yield json.dumps({"event": "plan", "data": {"tasks": plan_tasks}}) + "\n"
    # Dorongan "jangan berhenti" bila model berhenti sebelum rencana tuntas.
    # Dibatasi agar tak jadi loop tak berujung pada model yang keras kepala.
    continue_nudges = 0
    MAX_CONTINUE_NUDGES = 6
    # Anggaran hanya dinaikkan SEKALI per run: kalau anggaran tiga kali lipat
    # masih habis untuk berpikir, menaikkan lagi cuma memperlama kegagalan.
    budget_escalated = False
    cumulative_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    max_iterations = mode_cfg["max_iterations"]
    temperature = mode_cfg["temperature"]

    for iteration in range(max_iterations):
        force_answer = iteration >= max_iterations - 1
        api_kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
        }
        if not force_answer:
            api_kwargs["tools"] = tools
            # Paksa rencana dulu: sebelum plan disubmit, model wajib panggil tool.
            api_kwargs["tool_choice"] = "required" if not plan_submitted else "auto"

        try:
            stream = await client.chat.completions.create(**api_kwargs)
        except Exception as e:  # noqa: BLE001
            # Ketahanan gateway BYO (mis. model gratis): (a) sebagian menolak
            # tool_choice "required" → turunkan ke "auto" sekali; (b) error
            # transien (timeout/rate-limit) → ulang sekali dengan jeda. Kegagalan
            # di tengah run TIDAK boleh mematikan proses tanpa kabar ke pengguna.
            last_err = e
            if api_kwargs.get("tool_choice") == "required":
                logger.info("tool_choice 'required' ditolak gateway; fallback ke 'auto'.")
                api_kwargs["tool_choice"] = "auto"
            stream = None
            for _retry in range(2):
                try:
                    await asyncio.sleep(1.2)
                    stream = await client.chat.completions.create(**api_kwargs)
                    break
                except Exception as e2:  # noqa: BLE001
                    last_err = e2
            if stream is None:
                # str(exception) bisa berupa blob JSON 400 mentah dari gateway
                # (mis. provider free membalas assistant KOSONG lalu dibungkus
                # 'bad_request'), atau bahkan string kosong. Selalu LOG penuh —
                # jalur "belum ada rencana" dulu diam sehingga kegagalan tak
                # terlihat di server — dan JANGAN muntahkan blob itu apa adanya ke
                # UI; ringkas jadi pesan yang bisa ditindaklanjuti.
                msg = str(last_err) or type(last_err).__name__
                logger.error(
                    "agent_run: gagal memanggil model di iterasi %s "
                    "(plan_submitted=%s, model=%s): %s",
                    iteration, plan_submitted, model_name, msg,
                )
                if not plan_submitted:
                    # Sebagian kemajuan mungkin sudah ada (mis. cite_list jalan),
                    # tapi rencana belum tersusun. Kegagalan tipikal di sini:
                    # model/endpoint tak menangani pemanggilan tool beruntun
                    # (giliran berisi pesan hasil-tool) dengan andal.
                    detail = (
                        "Model gagal melanjutkan setelah langkah awal — endpoint "
                        "membalas galat saat giliran tool berikutnya. Ini biasanya "
                        "berarti model/endpoint tidak menangani tool-calling "
                        "beruntun dengan andal (umum pada model gratis). Coba "
                        "jalankan lagi, atau pilih model lain yang mendukung "
                        "tool-calling di Pengaturan."
                    )
                else:
                    # Sudah menulis sebagian → beri tahu terputus, jangan diam.
                    detail = (
                        "Koneksi ke model terputus di tengah pekerjaan; sebagian "
                        "sudah ditulis ke dokumen. Klik Kerjakan lagi untuk "
                        "melanjutkan bagian yang belum selesai."
                    )
                yield json.dumps({"event": "error", "data": detail}) + "\n"
                break

        tool_calls: dict[int, dict[str, Any]] = {}
        content_buffer = ""
        reasoning_buffer = ""
        finish_reason = None
        dsml_seen = False

        async for chunk in stream:
            if hasattr(chunk, "usage") and chunk.usage:
                cumulative_usage["prompt_tokens"] += getattr(chunk.usage, "prompt_tokens", 0) or 0
                cumulative_usage["completion_tokens"] += getattr(chunk.usage, "completion_tokens", 0) or 0
                cumulative_usage["total_tokens"] += getattr(chunk.usage, "total_tokens", 0) or 0
                if cumulative_usage["total_tokens"] > 0:
                    yield json.dumps({"event": "usage", "data": cumulative_usage}) + "\n"

            if chunk.choices and getattr(chunk.choices[0], "finish_reason", None):
                finish_reason = chunk.choices[0].finish_reason

            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            if delta.content:
                content_buffer += delta.content
                # Deteksi markup tool mentah (DSML) untuk pemulihan tool-call di
                # gateway yang tak mengurainya. Teks TIDAK dialirkan inline di sini:
                # ia baru dipancarkan sebagai Ringkasan di giliran final (lihat
                # bawah), supaya prosa "berhenti prematur" yang akan didorong-lanjut
                # tidak bocor ke kotak ringkasan.
                if not dsml_seen and _DSML_START.search(content_buffer):
                    dsml_seen = True

            # Jejak penalaran datang dengan nama field berbeda tergantung gateway:
            # OpenAI-style `reasoning_content`, atau `reasoning` (dipakai router
            # BYO proyek ini — terukur pada model oc/*). Tanpa menangkap keduanya,
            # reasoning_buffer selalu kosong pada model penalar, sehingga (a) panel
            # FE tampak beku selama model berpikir dan (b) jaring pengaman anggaran
            # di bawah — yang mensyaratkan reasoning_buffer terisi — tak pernah
            # aktif, membuat model penalar berhenti diam-diam saat anggaran habis.
            _reason = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
            )
            if not _reason:
                _extra = getattr(delta, "model_extra", None) or {}
                _reason = _extra.get("reasoning_content") or _extra.get("reasoning")
            if _reason:
                reasoning_buffer += _reason
                yield json.dumps({"event": "reasoning", "data": _reason}) + "\n"

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.index not in tool_calls:
                        tool_calls[tc.index] = {
                            "id": tc.id or f"tc_{uuid.uuid4().hex[:10]}",
                            "type": tc.type or "function",
                            "function": {
                                "name": tc.function.name if tc.function and tc.function.name else "",
                                "arguments": tc.function.arguments if tc.function and tc.function.arguments else "",
                            },
                        }
                    elif tc.function and tc.function.arguments:
                        tool_calls[tc.index]["function"]["arguments"] += tc.function.arguments

        # Pulihkan tool-call dari markup mentah bila gateway tak mengurainya.
        if dsml_seen and not tool_calls:
            recovered = _parse_dsml_tool_calls(content_buffer)
            if recovered:
                tool_calls = dict(enumerate(recovered))
        if dsml_seen:
            content_buffer = _strip_dsml(content_buffer)

        content_buffer = content_buffer.strip()

        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if content_buffer:
            assistant_msg["content"] = content_buffer
        if tool_calls:
            assistant_msg["tool_calls"] = list(tool_calls.values())
        else:
            assistant_msg["content"] = content_buffer or reasoning_buffer or ""
        messages.append(assistant_msg)

        if not tool_calls:
            # Anggaran habis untuk berpikir: giliran berakhir karena batas token
            # (`length`) tanpa satu aksara jawaban maupun tool call, padahal ada
            # jejak penalaran. Terukur pada model aktif proyek ini — dulu run
            # berakhir hanya dengan event ['reasoning','end'], sehingga panel
            # agentic tampak "tidak terjadi apa-apa" tanpa pesan apa pun.
            # Ditangani seperti agentic_writer.ask(): naikkan anggaran, coba
            # ulang sekali; kalau tetap kosong, LAPORKAN — jangan mati diam-diam.
            if (
                finish_reason == "length"
                and not content_buffer
                and reasoning_buffer
                and not force_answer
            ):
                # Giliran ini tidak menghasilkan apa pun; jangan tinggalkan pesan
                # asisten kosong di riwayat.
                messages.pop()
                if not budget_escalated:
                    naik = _anggaran_naik(max_tokens, context_window)
                    if naik > max_tokens:
                        logger.info(
                            "agent_run: anggaran %s token habis untuk penalaran "
                            "(%s aksara) pada %s; diulang dengan %s token.",
                            max_tokens, len(reasoning_buffer), model_name, naik,
                        )
                        max_tokens = naik
                        budget_escalated = True
                        continue
                yield json.dumps({
                    "event": "error",
                    "data": (
                        # Pesan dibedakan seperti pada kegagalan koneksi di atas:
                        # menyebut "tanpa rencana" padahal sebagian bab sudah
                        # ditulis akan menyesatkan.
                        (
                            "Model menghabiskan seluruh anggaran token untuk "
                            "penalaran tanpa menghasilkan rencana kerja. Coba mode "
                            "'cepat', perpendek instruksi, atau pilih model yang "
                            "bukan model penalaran di Pengaturan."
                        )
                        if not plan_submitted
                        else (
                            "Anggaran token habis untuk penalaran di tengah "
                            "pekerjaan; sebagian sudah ditulis ke dokumen. Klik "
                            "Kerjakan lagi untuk melanjutkan bagian yang belum "
                            "selesai, atau coba mode 'cepat'."
                        )
                    ),
                }) + "\n"
                break

            # FASE PROPOSE: begitu model berhenti memanggil tool, tahap usul
            # rencana selesai. JANGAN dorong-lanjut menulis (Guard B/C di bawah
            # hanya untuk fase execute) — pancarkan brainstorm model (atau ajakan
            # default bila kosong) lalu tutup. Dokumen tak pernah tersentuh karena
            # tool tulis diblokir sepanjang fase ini (lihat Guard D di bawah).
            if phase == "propose":
                if content_buffer:
                    yield json.dumps({"event": "text", "data": content_buffer}) + "\n"
                elif plan_submitted:
                    yield json.dumps({
                        "event": "text",
                        "data": "Itu rencana yang saya usulkan. Setuju untuk saya kerjakan?",
                    }) + "\n"
                else:
                    yield json.dumps({
                        "event": "error",
                        "data": (
                            "Model belum menghasilkan rencana. Coba perjelas "
                            "instruksi atau pilih model lain di Pengaturan."
                        ),
                    }) + "\n"
                break

            # Model berhenti memanggil tool. Kalau rencana BELUM tuntas (masih ada
            # tugas pending/running) dan kita belum di iterasi paksa-jawab, ini
            # berhenti prematur — dorong lanjut, jangan biarkan mati di tengah.
            pending = [t for t in plan_tasks if t.get("status") in ("pending", "running")]
            if (
                plan_submitted
                and pending
                and not force_answer
                and continue_nudges < MAX_CONTINUE_NUDGES
            ):
                continue_nudges += 1
                sisa = "; ".join(f"#{t['index']}: {t['title']}" for t in pending[:8])
                messages.append({
                    "role": "user",
                    "content": (
                        "JANGAN berhenti — rencana belum selesai. Tugas yang masih "
                        f"belum 'done': {sisa}. Lanjutkan sekarang: panggil "
                        "set_task_status('running') lalu doc_insert untuk tugas "
                        "berikutnya. Jangan menulis ringkasan sampai SEMUA tugas 'done'."
                    ),
                })
                continue

            # Rencana belum pernah dibuat, tapi model sudah menulis prosa —
            # biasanya balik bertanya "dokumen mana?" padahal isi dokumen SUDAH
            # ada di konteks. Terukur pada model aktif proyek ini: model memanggil
            # `read_document` dengan argumen asal, gagal, lalu bertanya; prosanya
            # dulu dipancarkan sebagai Ringkasan dan run berhenti tanpa satu baris
            # ditulis — dari sisi pengguna "tidak terjadi apa-apa". Gateway juga
            # tidak selalu menghormati tool_choice "required", jadi paksaan di
            # tingkat API tidak bisa diandalkan; dorong lewat pesan.
            if (
                not plan_submitted
                and not force_answer
                and continue_nudges < MAX_CONTINUE_NUDGES
            ):
                continue_nudges += 1
                messages.append({
                    "role": "user",
                    "content": (
                        "JANGAN bertanya dan JANGAN menjawab dengan prosa. Isi "
                        "dokumen yang disunting SUDAH diberikan di konteks pesan "
                        "pertama — kamu tidak perlu memanggil `read_document` dan "
                        "tidak perlu nama berkas. Untuk memeriksa isinya pakai "
                        "`find_in_document`. Sekarang panggil `submit_plan` dengan "
                        "daftar tugas konkret, lalu kerjakan tugasnya dengan "
                        "`doc_insert`/`doc_replace`. Kalau instruksinya kurang "
                        "detail, ambil asumsi yang wajar dan tuliskan asumsi itu "
                        "di ringkasan akhir."
                    ),
                })
                continue

            # Giliran final (rencana tuntas / paksa-jawab): baru sekarang prosa model
            # dipancarkan sebagai RINGKASAN — sekali, utuh. Ditahan sampai di sini
            # supaya prosa "berhenti prematur" yang didorong-lanjut tak ikut bocor.
            if content_buffer:
                yield json.dumps({"event": "text", "data": content_buffer}) + "\n"
            elif not plan_submitted:
                # Tak ada rencana, tak ada tool, tak ada prosa — pengguna berhak
                # tahu kenapa panel kosong.
                yield json.dumps({
                    "event": "error",
                    "data": (
                        "Model tidak menghasilkan rencana kerja maupun jawaban "
                        f"(alasan berhenti: {finish_reason or 'tidak diketahui'}). "
                        "Model ini mungkin tidak mendukung tool calling; coba model "
                        "lain di Pengaturan."
                    ),
                }) + "\n"
            break

        # Eksekusi tiap tool call.
        for tc in tool_calls.values():
            tc_id = tc["id"]
            tc_name = tc["function"]["name"]
            tc_args_str = tc["function"]["arguments"] or "{}"
            args_rusak = False
            try:
                tc_args = json.loads(tc_args_str)
            except Exception:
                # Argumen tool terpotong (umumnya finish_reason="length" pada
                # doc_insert berisi satu bab). Dulu ini diam-diam menjadi {},
                # lalu doc_insert melaporkan "Menyisipkan 0 karakter" sebagai
                # SUKSES — model menandai tugasnya 'done' dan lanjut, padahal
                # dokumen tak berubah. Sekarang dilaporkan gagal ke model dan UI.
                tc_args = {}
                args_rusak = True

            is_fe = tc_name in _FE_TOOLS

            # Tool tulis DITOLAK sebelum rencana disubmit. `tool_choice="required"`
            # memaksa model memanggil tool, tapi tidak bisa memaksa tool yang MANA:
            # model aktif proyek ini terukur langsung memanggil `doc_insert` dua kali
            # (isi identik, 885 aksara) sebelum `submit_plan`, lalu sekali lagi
            # sesudahnya — `find_in_document` menemukan 3 kecocokan, artinya paragraf
            # yang sama masuk tiga kali ke dokumen pengguna. Aturan "submit_plan
            # lebih dulu" sudah ada di prompt sistem tapi tidak pernah ditegakkan,
            # dan tool tulis dieksekusi frontend (fe=true) sehingga tulisan liar itu
            # benar-benar mengubah dokumen. Penolakan di sini membuat model
            # merencanakan dulu tanpa merusak apa pun.
            if is_fe and (not plan_submitted or phase == "propose"):
                logger.info(
                    "agent_run: %s ditolak (fase=%s, plan_submitted=%s).",
                    tc_name, phase, plan_submitted,
                )
                _reason = (
                    (
                        "Ini TAHAP USUL RENCANA — dokumen belum boleh disentuh. "
                        "Jangan menulis; cukup usulkan rencana lalu berhenti dan "
                        "tunggu persetujuan pengguna."
                    )
                    if phase == "propose"
                    else (
                        "Belum ada rencana. Panggil submit_plan lebih dulu, baru "
                        "menulis. Tidak ada perubahan yang diterapkan ke dokumen."
                    )
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "status": "rejected",
                        "reason": _reason,
                    }),
                })
                continue

            # Umumkan pemanggilan tool (kontrol tidak perlu, ditangani khusus).
            if tc_name not in _CONTROL_TOOLS:
                yield json.dumps({
                    "event": "tool_call",
                    "data": {"id": tc_id, "name": tc_name, "args": tc_args, "fe": is_fe},
                }) + "\n"

            if args_rusak:
                logger.warning(
                    "agent_run: argumen tool '%s' terpotong (%s aksara, finish_reason=%s)",
                    tc_name, len(tc_args_str), finish_reason,
                )
                yield json.dumps({
                    "event": "tool_result",
                    "data": {
                        "id": tc_id,
                        "name": tc_name,
                        "ok": False,
                        "summary": "Argumen tool terpotong — isi tidak ditulis.",
                    },
                }) + "\n"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "status": "error",
                        "reason": (
                            "Argumen terpotong karena batas token; TIDAK ada yang "
                            "ditulis. Panggil ulang tool ini dengan isi yang jauh "
                            "lebih pendek (satu sub-bagian saja)."
                        ),
                    }),
                })
                continue

            result_payload, tool_result_str = await _dispatch_tool(
                tc_name, tc_args, shadow, db, user_id, plan_tasks,
            )

            # Event terstruktur untuk kontrol/plan.
            if tc_name == "submit_plan":
                plan_submitted = True
                yield json.dumps({"event": "plan", "data": {"tasks": plan_tasks}}) + "\n"
            elif tc_name == "set_task_status":
                yield json.dumps({"event": "task_status", "data": result_payload}) + "\n"
            elif tc_name not in _CONTROL_TOOLS:
                yield json.dumps({
                    "event": "tool_result",
                    "data": {"id": tc_id, "name": tc_name, "ok": result_payload.get("ok", True),
                             "summary": result_payload.get("summary", "")},
                }) + "\n"

            messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_result_str})

    yield json.dumps({"event": "end", "data": {}}) + "\n"


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    shadow: _ShadowDoc,
    db: AsyncSession,
    user_id: uuid.UUID,
    plan_tasks: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Jalankan satu tool. Kembalikan (payload_event, string_untuk_model).

    Tool tulis (FE-intercept) tidak menyentuh editor di sini — hanya memperbarui
    buffer bayangan dan mengembalikan sukses optimistik. Tool baca/kontrol
    dieksekusi sungguhan.
    """
    try:
        if name == "submit_plan":
            tasks = args.get("tasks") or []
            plan_tasks.clear()
            for i, t in enumerate(tasks):
                title = t if isinstance(t, str) else str(t)
                plan_tasks.append({"index": i, "title": title.strip()[:200], "status": "pending"})
            return ({"ok": True}, json.dumps({"status": "plan diterima", "task_count": len(plan_tasks)}))

        if name == "set_task_status":
            idx = int(args.get("task_index", -1))
            st = str(args.get("status", "")).strip()
            note = str(args.get("note", "")).strip()
            if 0 <= idx < len(plan_tasks) and st in ("running", "done", "failed", "pending"):
                plan_tasks[idx]["status"] = st
            payload = {"index": idx, "status": st}
            if note:
                payload["note"] = note
            return (payload, json.dumps({"status": "ok"}))

        if name == "doc_insert":
            md = str(args.get("markdown", ""))
            if not md.strip():
                # Sisipan kosong tak pernah berguna, dan melaporkannya sebagai
                # sukses ("Menyisipkan 0 karakter") membuat model yakin tugasnya
                # selesai padahal dokumen tak berubah.
                return ({"ok": False, "summary": "Tidak ada isi untuk disisipkan."},
                        json.dumps({
                            "status": "error",
                            "reason": "Argumen 'markdown' kosong — kirim isi yang mau ditulis.",
                        }))
            anchor = args.get("anchor_text") or None
            placement = str(args.get("placement", "after"))
            shadow.insert(md, anchor, placement)
            loc = f"setelah \"{anchor[:40]}\"" if anchor else "di akhir dokumen"
            return ({"ok": True, "summary": f"Menyisipkan {len(md)} karakter {loc}."},
                    json.dumps({"status": "applied", "chars": len(md)}))

        if name == "doc_replace":
            find = str(args.get("find", ""))
            replace = str(args.get("replace", ""))
            all_occ = bool(args.get("all", False))
            n = shadow.replace(find, replace, all_occ)
            if n == 0:
                return ({"ok": False, "summary": f"Teks \"{find[:40]}\" tak ditemukan."},
                        json.dumps({"status": "not_found", "hint": "Pakai find_in_document untuk cek teks jangkar."}))
            return ({"ok": True, "summary": f"Mengganti {n} kemunculan."},
                    json.dumps({"status": "applied", "replaced": n}))

        if name == "cite_insert":
            anchor = str(args.get("anchor_text", ""))
            title = str(args.get("title", ""))
            authors = args.get("authors") or []
            if not title or not authors:
                return ({"ok": False, "summary": "Sitasi ditolak: metadata sumber tak lengkap."},
                        json.dumps({"status": "rejected", "reason": "title & authors wajib; jangan mengarang."}))
            # Tandai di bayangan agar penalaran lanjutan konsisten.
            shadow.insert(f"[sitasi: {title}]", anchor or None, "after")
            return ({"ok": True, "summary": f"Menyisipkan sitasi \"{title[:50]}\"."},
                    json.dumps({"status": "applied"}))

        if name == "find_in_document":
            query = str(args.get("query", ""))
            hits = shadow.find(query)
            if not hits:
                return ({"ok": True, "summary": f"'{query[:40]}' tak ditemukan."},
                        json.dumps({"matches": [], "message": "Tidak ditemukan."}))
            return ({"ok": True, "summary": f"{len(hits)} kecocokan untuk '{query[:40]}'."},
                    json.dumps({"matches": hits}))

        if name == "cite_add":
            # Nomor sitasi ditentukan SERVER lalu dikembalikan ke model; itu yang
            # membuat `[n]` sah dan Daftar Pustaka bisa terisi otomatis.
            out = await cite_add(db, user_id, **args)
            data = json.loads(out)
            return (
                {"ok": True, "summary": f"Sumber \"{data.get('title', '')[:44]}\" → {data.get('sitasi', '')}"},
                out,
            )

        if name == "cite_list":
            out = await cite_list(db, user_id)
            data = json.loads(out)
            return ({"ok": True, "summary": f"{data.get('jumlah', 0)} sumber di perpustakaan."}, out)

        if name == "ref_read":
            out = await ref_read(db, user_id, **args)
            data = json.loads(out)
            if data.get("error"):
                return ({"ok": False, "summary": str(data["error"])[:80]}, out)
            return (
                {"ok": True, "summary": f"Membaca sumber {data.get('sitasi', '')} ({data.get('sumber', '')})."},
                out,
            )

        if name == "cite_bibliography":
            # Naskah dipindai dari buffer bayangan, bukan dari klaim model: nomor
            # yang masuk Daftar Pustaka harus yang benar-benar tertulis.
            md, dipakai, asing = await bibliografi_markdown(db, user_id, shadow.to_text())
            if not dipakai:
                return (
                    {"ok": False, "summary": "Belum ada sitasi [n] yang sah di naskah."},
                    json.dumps({
                        "status": "kosong",
                        "nomor_tak_dikenal": asing,
                        "pesan": (
                            "Tidak ada sitasi [n] yang cocok dengan perpustakaan. Simpan "
                            "sumber lewat cite_add lalu tulis nomor yang dikembalikannya."
                        ),
                    }, ensure_ascii=False),
                )
            return (
                {"ok": True, "summary": f"Daftar Pustaka siap: {len(dipakai)} entri {dipakai}."},
                json.dumps({
                    "status": "siap",
                    "nomor_dipakai": dipakai,
                    "nomor_tak_dikenal": asing,
                    "markdown": md,
                    "pesan": (
                        "Sisipkan nilai 'markdown' APA ADANYA lewat doc_insert tanpa "
                        "anchor_text (akhir dokumen). Jangan diubah atau disusun ulang."
                    ),
                }, ensure_ascii=False),
            )

        if name == "read_document":
            out = await read_document(db, user_id, **args)
            return ({"ok": True, "summary": "Membaca dokumen referensi."}, out)

        if name == "search_in_document":
            out = await search_in_document(db, user_id, **args)
            return ({"ok": True, "summary": "Mencari di dokumen referensi."}, out)

        if name == "search_web":
            args.setdefault("max_results", 5)
            out = await search_web(**args)
            return ({"ok": True, "summary": f"Mencari web: {args.get('query', '')[:40]}"}, out)

        if name == "arxiv_search":
            args.setdefault("max_results", 5)
            out = await arxiv_search(**args)
            return ({"ok": True, "summary": f"Mencari arXiv: {args.get('query', '')[:40]}"}, out)

        if name == "fetch_webpage":
            out = await fetch_webpage(**args)
            return ({"ok": True, "summary": "Membuka halaman web."}, out)

        return ({"ok": False, "summary": f"Tool tak dikenal: {name}"},
                json.dumps({"error": f"Unknown tool: {name}"}))
    except SitasiError as e:
        # Metadata sumber tak lengkap: ini penolakan yang bisa diperbaiki model
        # (cari sumbernya dulu), bukan galat sistem. Dibedakan dari Exception
        # generik supaya pesannya memandu, bukan sekadar "Tool gagal".
        logger.info("agent_run cite ditolak: %s", e)
        return ({"ok": False, "summary": f"Sitasi ditolak: {e}"},
                json.dumps({"status": "rejected", "reason": str(e)}))
    except Exception as e:  # noqa: BLE001
        logger.exception(f"agent_run tool '{name}' gagal")
        return ({"ok": False, "summary": f"Tool {name} gagal: {e}"},
                json.dumps({"error": f"Tool execution failed: {e}"}))
