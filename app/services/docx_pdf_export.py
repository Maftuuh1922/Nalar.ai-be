"""Cetak DOCX → PDF memakai LibreOffice headless (ekspor mode Word).

Ekspor PDF mode Word harus "persis seperti di layar": editor SuperDoc mengedit
DOCX kerja, jadi PDF-nya dicetak dari DOCX kerja yang sama — bukan dibangun
ulang dari buffer LaTeX (template kampus, margin dipaku) yang menghasilkan
tata letak berbeda.

Polanya menyalin `_convert_via_libreoffice` di `pdf_docx_import.py` yang sudah
terbukti di Windows: `soffice.com`, PYTHONHOME dibersihkan ke python-core,
profil UserInstallation unik per panggilan (aman untuk konversi bersamaan),
Popen + `taskkill /T /F` saat timeout (subprocess.run timeout TIDAK membunuh
child di Windows), profil dibersihkan di `finally`. Bedanya: `--convert-to pdf`
TANPA `--infilter` (infilter khusus untuk MEMBUKA PDF sebagai Writer, tidak
relevan saat membuka DOCX).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid as _uuid
from pathlib import Path

from app.services.pdf_docx_import import _soffice_bin

log = logging.getLogger("docx_pdf_export")


def tersedia() -> bool:
    """True bila LibreOffice terpasang (satu-satunya mesin cetak jalur ini)."""
    return _soffice_bin() is not None


def docx_to_pdf(docx_path: str | Path, pdf_path: str | Path) -> tuple[bool, str]:
    """Konversi DOCX → PDF via LibreOffice headless. Return (sukses, pesan).

    Tidak ada fallback: bila LibreOffice tak ada, pemanggil harus mengembalikan
    503 yang bisa ditindaklanjuti daripada mencetak PDF bermargin salah.
    """
    docx_path = Path(docx_path)
    pdf_path = Path(pdf_path)
    if not docx_path.is_file():
        return False, f"DOCX kerja tidak ditemukan: {docx_path}"

    soffice = _soffice_bin()
    if not soffice:
        return False, "LibreOffice tidak terinstal di lingkungan ini"

    out_dir = pdf_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # LibreOffice mengemas Python sendiri (python-core). Bila PYTHONHOME/
    # PYTHONPATH dari environment induk bocor ke subprocess, Python internal
    # LibreOffice gagal start dan filter export PDF tidak termuat. Bersihkan
    # keduanya dan arahkan PYTHONHOME ke python-core LibreOffice.
    lo_program = os.path.dirname(soffice)
    py_core = os.path.join(lo_program, "python-core-3.12.13")
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    clean_env["PYTHONHOME"] = py_core
    clean_env["PYTHONPATH"] = os.path.join(py_core, "lib")

    # Profil unik per panggilan → aman untuk konversi bersamaan (asyncio).
    profile_dir = Path(tempfile.gettempdir()) / f"lo_pdf_{_uuid.uuid4().hex[:12]}"
    cmd = [
        soffice,
        "--headless",
        # Bentuk SATU strip (`-env:`). LibreOffice 26.2+ MENOLAK bentuk dua strip
        # (`--env:`) dengan "Error in option" lalu mencetak teks bantuan ke
        # stdout dan keluar rc=1 — persis yang dulu menggagalkan ekspor PDF.
        f"-env:UserInstallation=file:///{profile_dir.as_posix()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(docx_path),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=clean_env,
        )
        try:
            stdout, stderr = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            # subprocess.run timeout TIDAK membunuh child di Windows —
            # kill tree eksplisit (taskkill /T /F) lalu tunggu.
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            return False, "LibreOffice timeout (>600s) — proses di-kill"

        if proc.returncode != 0:
            # LibreOffice menulis error argumen ke stdout, bukan stderr — sertakan
            # keduanya supaya pesan 502 tidak kosong dan bisa ditindaklanjuti.
            pesan = (stderr or "").strip() or (stdout or "").strip()
            return False, f"LibreOffice returncode {proc.returncode}: {pesan[-300:]}"
        produced = out_dir / (docx_path.stem + ".pdf")
        if not produced.exists():
            return False, "LibreOffice selesai tapi PDF tidak dihasilkan"
        if produced.resolve() != pdf_path.resolve():
            shutil.move(str(produced), str(pdf_path))
        return True, "OK"
    except FileNotFoundError:
        return False, "LibreOffice tidak ditemukan"
    except Exception as exc:  # noqa: BLE001
        return False, f"LibreOffice: {exc}"
    finally:
        # Bersihkan profil temporary (sukses maupun gagal) — jangan menumpuk
        # folder di temp dir setelah banyak konversi.
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
