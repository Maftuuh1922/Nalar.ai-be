"""Konversi PDF → DOCX terstruktur untuk pipeline IMPORT Co-Writer (PRD P0).

Menggantikan assembly manual (PyMuPDF + python-docx) yang terbukti merusak
urutan/tata letak/heading/gambar saat dokumen dibuka di editor (gejala sama
di WordEditor lama & Syncfusion = akar masalah ada di tahap konversi).

Alur:
1. Coba `pdf2docx.Converter` (built-in: deteksi heading, tabel, gambar + posisi,
   menghasilkan paragraf normal + tabel yang bisa diedit).
2. Jika gagal / hasil tidak valid → fallback LibreOffice headless (`soffice`).
3. Validasi hasil: bisa dibuka python-docx, jumlah konten proporsional.
4. Post-process halaman cover (rata tengah) bila memungkinkan.
5. Catat metode yang berhasil (untuk observability).

CATATAN 2026-08-10 (eksperimen LibreOffice sebagai primary):
- LibreOffice + `--infilter=writer_pdf_import` menghasilkan teks TANPA bug
  run-spacing/hyphenation (0 vs 86/20 di pdf2docx), TAPI:
  - Setiap halaman PDF menjadi text box (`w:txbxContent`) — python-docx tidak
    melihat teks (0 paragraf level-akar) → editor tampak kosong.
  - Teks DUPLIKAT (tiap baris muncul 2×) — overlay shape + teks.
  - TABEL HILANG (0 tabel vs 95 di pdf2docx) — tabel PDF jadi gambar/text box.
  - Konversi jauh lebih lambat (322s vs 288s utk 89 hal, ODT bahkan timeout).
  Kesimpulan: LibreOffice TIDAK layak jadi primary utk PDF→DOCX. pdf2docx
  tetap primary; `_fix_run_spacing` menangani bug spasi di output pdf2docx.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pdf_docx_import")


@dataclass
class ConversionResult:
    method: str  # "pdf2docx" | "libreoffice" | "failed"
    duration_sec: float
    error: str | None = None
    warnings: list[str] | None = None
    detail: str | None = None


def _soffice_bin() -> str | None:
    """Lokasi executable LibreOffice (Windows / Linux).

    Di Windows, `soffice.com` adalah console wrapper yang benar untuk
    subprocess (menunggu proses selesai & mengembalikan exit code);
    `soffice.exe` adalah GUI stub yang bisa return sebelum selesai.
    """
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.com",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/local/bin/soffice",
        "soffice",
    ]
    for c in candidates:
        if c == "soffice":
            if shutil.which("soffice"):
                return "soffice"
            continue
        if os.path.isfile(c):
            return c
    return None


def _valid_docx(path: Path) -> tuple[bool, str]:
    """Buka DOCX via python-docx; kembalikan (valid, alasan bila tidak)."""
    try:
        import docx

        d = docx.Document(str(path))
        n_par = len(d.paragraphs)
        n_tab = len(d.tables)
        if n_par == 0 and n_tab == 0:
            return False, f"DOCX kosong (0 paragraf, 0 tabel)"
        return True, f"OK ({n_par} paragraf, {n_tab} tabel)"
    except Exception as exc:  # noqa: BLE001
        return False, f"tidak dapat dibuka python-docx: {exc}"


def _convert_via_pdf2docx(pdf_path: Path, docx_path: Path) -> str:
    """Konversi via pdf2docx. Return pesan error bila gagal, else ''."""
    try:
        from pdf2docx import Converter

        cv = Converter(str(pdf_path))
        try:
            cv.convert(str(docx_path), start=0, end=None)
        finally:
            cv.close()
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"pdf2docx: {exc}"


def _convert_via_libreoffice(pdf_path: Path, docx_path: Path) -> str:
    """Konversi via LibreOffice headless. Return pesan error bila gagal.

    WAJIB pakai ``--infilter=writer_pdf_import``: tanpa infilter, LibreOffice
    membuka PDF sebagai *Draw document* yang tidak punya filter export DOCX
    ("no export filter"). Dengan infilter ini PDF dibuka sebagai *Writer
    document* sehingga DOCX export menghasilkan dokumen teks terstruktur.

    Safety (2026-08-10):
    - timeout eksplisit + kill tree proses soffice bila menggantung
      (`subprocess.run` timeout saja TIDAK membunuh child di Windows —
      pakai Popen + taskkill /T).
    - isolasi user-profile per panggilan (`-env:UserInstallation=file:///...`)
      supaya konversi bersamaan dari request berbeda tidak lock-conflict.
    - folder profile temporary dibersihkan setelah selesai (sukses/gagal).
    """
    soffice = _soffice_bin()
    if not soffice:
        return "LibreOffice tidak terinstal di lingkungan ini"
    out_dir = docx_path.parent
    # LibreOffice mengemas Python sendiri (python-core). Bila PYTHONHOME/
    # PYTHONPATH dari environment induk (mis. MSYS hermes-agent) bocor ke
    # subprocess, Python internal LibreOffice gagal start ("Could not find
    # platform independent libraries") dan filter DOCX tidak termuat.
    # Bersihkan keduanya dan arahkan PYTHONHOME ke python-core LibreOffice.
    lo_program = os.path.dirname(soffice)
    py_core = os.path.join(lo_program, "python-core-3.12.13")
    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    clean_env["PYTHONHOME"] = py_core
    clean_env["PYTHONPATH"] = os.path.join(py_core, "lib")

    # Profile unik per panggilan → aman untuk konversi bersamaan (asyncio).
    import tempfile
    import uuid as _uuid

    profile_dir = Path(tempfile.gettempdir()) / f"lo_profile_{_uuid.uuid4().hex[:12]}"
    cmd = [
        soffice,
        "--headless",
        # Bentuk SATU strip (`-env:`). LibreOffice 26.2+ MENOLAK bentuk dua strip
        # (`--env:`) dengan "Error in option" → rc=1 (teks bantuan ke stdout),
        # sehingga fallback impor ini mati diam-diam di versi baru.
        f"-env:UserInstallation=file:///{profile_dir.as_posix()}",
        "--infilter=writer_pdf_import",
        "--convert-to",
        "docx",
        "--outdir",
        str(out_dir),
        str(pdf_path),
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
            return "LibreOffice timeout (>600s) — proses di-kill"

        result_rc = proc.returncode
        produced = out_dir / (pdf_path.stem + ".docx")
        if result_rc != 0:
            # LibreOffice menulis error argumen ke stdout, bukan stderr —
            # sertakan keduanya supaya pesan tidak kosong.
            pesan = (stderr or "").strip() or (stdout or "").strip()
            return f"LibreOffice returncode {result_rc}: {pesan[-300:]}"
        if not produced.exists():
            return "LibreOffice selesai tapi DOCX tidak dihasilkan"
        if produced.resolve() != docx_path.resolve():
            shutil.move(str(produced), str(docx_path))
        return ""
    except FileNotFoundError:
        return "LibreOffice tidak ditemukan"
    except Exception as exc:  # noqa: BLE001
        return f"LibreOffice: {exc}"
    finally:
        # Bersihkan profile temporary (sukses maupun gagal) — jangan menumpuk
        # folder di temp dir setelah banyak konversi.
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def convert_pdf_to_docx(
    pdf_path: str | Path,
    docx_path: str | Path,
    *,
    min_ratio_threshold: float = 0.05,
) -> tuple[bool, ConversionResult]:
    """Konversi PDF → DOCX dengan fallback + validasi.

    Urutan metode: LibreOffice headless (primary, kualitas terbaik) →
    pdf2docx (fallback) → failed. Returns (success, result).
    """
    pdf_path = Path(pdf_path)
    docx_path = Path(docx_path)
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()

    # estimasi jumlah halaman sumber (untuk validasi proporsional)
    try:
        import fitz

        src_pages = fitz.open(str(pdf_path)).page_count
    except Exception:  # noqa: BLE001
        src_pages = 0

    # 1) primary: pdf2docx (struktur terbaik: paragraf normal + tabel)
    err_pdf2docx = _convert_via_pdf2docx(pdf_path, docx_path)
    if not err_pdf2docx and docx_path.exists() and docx_path.stat().st_size > 0:
        ok, note = _valid_docx(docx_path)
        if ok:
            dur = time.monotonic() - start
            return (
                True,
                ConversionResult(
                    method="pdf2docx",
                    duration_sec=round(dur, 2),
                    detail=f"pdf2docx OK ({note})",
                ),
            )
        err_pdf2docx = f"pdf2docx hasil tidak valid: {note}"

    # bersihkan hasil tidak valid dari percobaan pertama
    if docx_path.exists():
        try:
            docx_path.unlink()
        except OSError:
            pass

    # 2) fallback: LibreOffice headless (kualitas teks baik, struktur text box)
    if _soffice_bin():
        err_lo = _convert_via_libreoffice(pdf_path, docx_path)
        if not err_lo and docx_path.exists() and docx_path.stat().st_size > 0:
            ok, note = _valid_docx(docx_path)
            if ok:
                dur = time.monotonic() - start
                return (
                    True,
                    ConversionResult(
                        method="libreoffice",
                        duration_sec=round(dur, 2),
                        detail=f"libreoffice fallback OK ({note})",
                    ),
                )
            err_lo = f"LibreOffice hasil tidak valid: {note}"
        if docx_path.exists():
            try:
                docx_path.unlink()
            except OSError:
                pass
    else:
        err_lo = "LibreOffice tidak terinstal di lingkungan ini"

    dur = time.monotonic() - start
    return (
        False,
        ConversionResult(
            method="failed",
            duration_sec=round(dur, 2),
            error=f"Semua metode gagal. pdf2docx: {err_pdf2docx} | LibreOffice: {err_lo}",
        ),
    )