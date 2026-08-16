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
ringkasan. Lihat [[nalar-cowriter-agentic-roadmap]] Fase A.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, AsyncGenerator

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agentic_chat import _parse_dsml_tool_calls, _strip_dsml, _DSML_START
from app.services.document_tools import (
    read_document,
    search_in_document,
    search_web,
    fetch_webpage,
    arxiv_search,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Mode eksekusi (default "seimbang"). Menyetel kedalaman loop, suhu, dan apakah
# riset web otonom diaktifkan. Tetap menghormati capability_tier di endpoint.
# --------------------------------------------------------------------------- #
_MODES: dict[str, dict[str, Any]] = {
    "cepat": {"max_iterations": 12, "temperature": 0.3, "web": False},
    "seimbang": {"max_iterations": 24, "temperature": 0.4, "web": True},
    "menyeluruh": {"max_iterations": 48, "temperature": 0.5, "web": True},
}
_DEFAULT_MODE = "seimbang"


def _mode_config(mode: str | None) -> dict[str, Any]:
    return _MODES.get((mode or _DEFAULT_MODE).lower(), _MODES[_DEFAULT_MODE])


# --------------------------------------------------------------------------- #
# Buffer teks bayangan — cermin ringan dokumen supaya find_in_document dan
# validasi jangkar melihat tulisan model sendiri dalam satu giliran. BUKAN
# sumber kebenaran; editor SuperDoc di frontend yang otoritatif.
# --------------------------------------------------------------------------- #
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
        count = 0
        for i, line in enumerate(self._lines):
            if find in line:
                n = line.count(find) if all_occurrences else 1
                self._lines[i] = line.replace(find, replace, -1 if all_occurrences else 1)
                count += n
                if not all_occurrences:
                    break
        return count

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
            "memperbaiki, atau menyunting kalimat yang sudah ada di dokumen."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "find": {"type": "string", "description": "Teks persis yang dicari."},
                "replace": {"type": "string", "description": "Teks pengganti."},
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


_SYSTEM_PROMPT = (
    "Kamu adalah ASISTEN AGENTIC penulis laporan akademik di dalam editor mirip "
    "Word. Kamu tidak sekadar menjawab — kamu BEKERJA: menyusun rencana lalu "
    "mengeksekusinya langsung ke dokumen pengguna lewat tool.\n\n"
    "ALUR WAJIB:\n"
    "1. PANGGIL `submit_plan` LEBIH DULU dengan daftar tugas konkret sesuai "
    "instruksi pengguna. Jangan menulis apa pun sebelum ini.\n"
    "2. Kerjakan tugas SATU per SATU sesuai urutan rencana. Untuk tiap tugas: "
    "panggil `set_task_status(index, 'running')`, lakukan pekerjaannya (tool "
    "tulis/riset), lalu `set_task_status(index, 'done')`. WAJIB menandai setiap "
    "tugas — jangan menulis isi tanpa memperbarui status tugasnya.\n"
    "3. Gunakan `doc_insert` untuk menambah isi, `doc_replace` untuk merapikan/"
    "memperbaiki teks yang ada, `cite_insert` untuk sitasi.\n"
    "4. Sebelum `doc_replace` atau `cite_insert`, pakai `find_in_document` untuk "
    "memastikan teks jangkar benar-benar ada. Kalau tidak ada, cari alternatif.\n"
    "5. Setelah SEMUA tugas berstatus 'done', berikan RINGKASAN akhir singkat "
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
    "berupa MARKDOWN (judul `##`, tebal `**`, daftar `-`). BUKAN LaTeX.\n"
    "- DILARANG mengarang fakta, angka, DOI, atau referensi. Untuk data yang belum "
    "diberikan pengguna, tulis penanda `[BUTUH DATA: ...]`; untuk klaim yang butuh "
    "rujukan, tulis `[SITASI: ...]` — JANGAN mengarang nomor sitasi atau nama "
    "penulis. Untuk sitasi hidup, hanya pakai sumber yang diverifikasi lewat riset; "
    "bila ragu, jangan menyisipkan sitasi dan sebutkan itu di ringkasan.\n"
    "- Rapikan hanya yang perlu; jangan menghapus isi pengguna tanpa alasan.\n"
    "- Efisien: jangan mengulang tool yang sama tanpa hasil baru."
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
    max_tokens: int = 4000,
    selection_text: str | None = None,
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

    context_note = ""
    if doc_context:
        # Beri model potret ringkas dokumen (dipotong): cukup untuk merencanakan &
        # mencari jangkar, tanpa membanjiri jendela konteks model murah.
        snippet = doc_context.strip()
        if len(snippet) > 6000:
            snippet = snippet[:3000] + "\n\n[...dipotong...]\n\n" + snippet[-2000:]
        context_note = f"\n\nPOTRET DOKUMEN SAAT INI (ringkas):\n{snippet}"
    if selection_text:
        context_note += f"\n\nTEKS YANG SEDANG DISOROT PENGGUNA:\n{selection_text.strip()[:2000]}"

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"INSTRUKSI: {instruction}{context_note}"},
    ]

    plan_tasks: list[dict[str, Any]] = []
    plan_submitted = False
    # Dorongan "jangan berhenti" bila model berhenti sebelum rencana tuntas.
    # Dibatasi agar tak jadi loop tak berujung pada model yang keras kepala.
    continue_nudges = 0
    MAX_CONTINUE_NUDGES = 6
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
                msg = str(last_err)
                if iteration == 0 or not plan_submitted:
                    # Belum ada kemajuan → laporkan sebagai error awal.
                    yield json.dumps({"event": "error", "data": msg}) + "\n"
                else:
                    # Sudah menulis sebagian → beri tahu terputus, jangan diam.
                    logger.error(f"agent_run iterasi {iteration} gagal total: {msg}")
                    yield json.dumps({
                        "event": "error",
                        "data": (
                            "Koneksi ke model terputus di tengah pekerjaan; sebagian "
                            "sudah ditulis ke dokumen. Klik Kerjakan lagi untuk "
                            "melanjutkan bagian yang belum selesai."
                        ),
                    }) + "\n"
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

            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                reasoning_buffer += delta.reasoning_content
                yield json.dumps({"event": "reasoning", "data": delta.reasoning_content}) + "\n"

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
            # Giliran final (rencana tuntas / paksa-jawab): baru sekarang prosa model
            # dipancarkan sebagai RINGKASAN — sekali, utuh. Ditahan sampai di sini
            # supaya prosa "berhenti prematur" yang didorong-lanjut tak ikut bocor.
            if content_buffer:
                yield json.dumps({"event": "text", "data": content_buffer}) + "\n"
            break

        # Eksekusi tiap tool call.
        for tc in tool_calls.values():
            tc_id = tc["id"]
            tc_name = tc["function"]["name"]
            tc_args_str = tc["function"]["arguments"] or "{}"
            try:
                tc_args = json.loads(tc_args_str)
            except Exception:
                tc_args = {}

            is_fe = tc_name in _FE_TOOLS

            # Umumkan pemanggilan tool (kontrol tidak perlu, ditangani khusus).
            if tc_name not in _CONTROL_TOOLS:
                yield json.dumps({
                    "event": "tool_call",
                    "data": {"id": tc_id, "name": tc_name, "args": tc_args, "fe": is_fe},
                }) + "\n"

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
    except Exception as e:  # noqa: BLE001
        logger.exception(f"agent_run tool '{name}' gagal")
        return ({"ok": False, "summary": f"Tool {name} gagal: {e}"},
                json.dumps({"error": f"Tool execution failed: {e}"}))
