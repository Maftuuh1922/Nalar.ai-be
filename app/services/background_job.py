"""Satu job background global (pip install / unduh model) dengan protokol cursor.

Halaman Document Parsing dan MinerU menampilkan SATU job pada satu waktu di
server (lihat komentar ``useBackgroundJob`` di frontend), jadi job disimpan
dalam satu slot global. Status di-poll tiap detik: baris log baru diambil
dengan ``cursor`` (index baris terakhir yang sudah dilihat klien).

``start_job`` memanggil ``asyncio.create_task`` sehingga harus dipanggil dari
dalam event loop (semua route FastAPI async memenuhi syarat).
"""

import asyncio
import os
import sys
from typing import Sequence

# state: idle | running | done | failed | cancelled
_JOB: dict = {
    "state": "idle",
    "kind": None,        # "install" | "models"
    "lines": [],
    "message": "",
    "_proc": None,
    "_task": None,
}

_MAX_LINES = 2000
_TRIM_TO = 1000


def _reset() -> None:
    _JOB["lines"] = []
    _JOB["message"] = ""
    _JOB["_proc"] = None
    _JOB["_task"] = None


async def _run_process(cmd: Sequence[str], kind: str, done_message: str, env: dict | None) -> None:
    _JOB["state"] = "running"
    _JOB["kind"] = kind
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    _JOB["_proc"] = proc
    try:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\r\n")
            if line:
                _JOB["lines"].append(line)
                if len(_JOB["lines"]) > _MAX_LINES:
                    _JOB["lines"] = _JOB["lines"][-_TRIM_TO:]
        rc = await proc.wait()
        if rc == 0:
            _JOB["state"] = "done"
            _JOB["message"] = done_message
        else:
            _JOB["state"] = "failed"
            _JOB["message"] = f"Proses keluar dengan kode {rc}"
    except asyncio.CancelledError:
        _JOB["state"] = "cancelled"
        _JOB["message"] = "Dibatalkan"
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    finally:
        _JOB["_proc"] = None
        _JOB["_task"] = None


def start_job(kind: str, cmd: Sequence[str], done_message: str, env: dict | None = None) -> tuple[bool, str]:
    """Mulai job baru. Kembalikan (ok, pesan_error). Tolak bila ada job jalan."""
    if _JOB["state"] == "running":
        return False, "Sebuah job sedang berjalan. Tunggu sampai selesai atau batalkan dulu."
    _reset()
    _JOB["state"] = "running"
    _JOB["kind"] = kind
    _JOB["_task"] = asyncio.create_task(_run_process(cmd, kind, done_message, env))
    return True, ""


def job_status(cursor: int = 0) -> dict:
    lines = _JOB["lines"]
    start = max(0, min(cursor, len(lines)))
    return {
        "state": _JOB["state"],
        "kind": _JOB["kind"],
        "lines": lines[start:],
        "next_cursor": len(lines),
        "message": _JOB["message"],
    }


def cancel_job() -> bool:
    """Batalkan job yang sedang berjalan (kill proses + cancel task)."""
    proc = _JOB.get("_proc")
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    task = _JOB.get("_task")
    if task is not None:
        task.cancel()
    return True


def pip_install_cmd(package: str) -> list[str]:
    """Perintah pip install yang memakai interpreter venv yang sama dengan server."""
    return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", package]


def env_with(endpoint: str | None = None) -> dict | None:
    """Salinan env proses + HF_ENDPOINT bila endpoint mirror diberikan."""
    if not endpoint:
        return None
    env = dict(os.environ)
    env["HF_ENDPOINT"] = endpoint
    return env
