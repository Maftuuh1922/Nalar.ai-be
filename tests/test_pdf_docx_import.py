"""Test pipeline PDF→DOCX impor: pdf_docx_import + docx_postprocess."""
import subprocess
import sys
from pathlib import Path

import docx
from docx.shared import Pt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.docx_postprocess import postprocess_docx
from app.services.pdf_docx_import import convert_pdf_to_docx


def _buat_docx_uji(path: Path) -> None:
    """DOCX kecil: judul cover 16pt + 'Bab 1' 16pt bold + sub-bab 14pt."""
    d = docx.Document()
    for teks, size, bold in [
        ("LAPORAN TUGAS AKHIR", 16, True),
        ("Nama: TEST", 12, False),
        ("Bab 1 PENDAHULUAN", 16, True),
        ("1.1 Latar Belakang", 14, True),
        ("Isi paragraf biasa.", 12, False),
    ]:
        p = d.add_paragraph(teks)
        for r in p.runs:
            r.font.size = Pt(size)
            r.font.bold = bold
    d.save(str(path))


def test_postprocess_heading_dan_cover(tmp_path: Path) -> None:
    src = tmp_path / "uji.docx"
    _buat_docx_uji(src)
    stats = postprocess_docx(src)

    assert stats["heading1"] >= 1, "Bab 1 harus jadi Heading 1"
    assert stats["heading2"] >= 1, "Sub-bab harus jadi Heading 2"
    assert stats["cover_center"] >= 1, "Cover harus rata tengah"

    d = docx.Document(str(src))
    styles = {p.text.strip(): p.style.name for p in d.paragraphs}
    assert styles.get("Bab 1 PENDAHULUAN", "").startswith("Heading 1")
    assert styles.get("1.1 Latar Belakang", "").startswith("Heading 2")


def test_convert_pdf_ke_docx_sukses_pdf2docx(tmp_path: Path, monkeypatch) -> None:
    """Tanpa LibreOffice → pdf2docx (fallback) yang menangani; hasil tetap valid."""
    from app.services import pdf_docx_import as mod

    monkeypatch.setattr(mod, "_soffice_bin", lambda: None)
    # pdf2docx memerlukan file; pakai file PDF nyata mini bila ada, else skip.
    pdf = Path(__file__).resolve().parents[2] / ".hermes/desktop-attachments/NALARAI_Laporan_Tugas_Akhir_Diperbarui.pdf"
    if not pdf.exists():
        import pytest

        pytest.skip("PDF uji tidak ditemukan")
    out = tmp_path / "out.docx"
    ok, res = convert_pdf_to_docx(pdf, out)
    assert ok is True
    assert res.method == "pdf2docx"  # LibreOffice dinonaktifkan → fallback pdf2docx
    assert out.exists() and out.stat().st_size > 0


def test_convert_pdf_ke_docx_libreoffice_fallback(tmp_path: Path, monkeypatch) -> None:
    """Bila pdf2docx gagal, LibreOffice menjadi fallback dan menghasilkan DOCX."""
    from app.services import pdf_docx_import as mod

    # Simulasikan soffice tersedia
    monkeypatch.setattr(mod, "_soffice_bin", lambda: "soffice")
    monkeypatch.setattr(mod, "_convert_via_pdf2docx", lambda pdf, out: "pdf2docx gagal simulasi")

    # Mock LibreOffice yang MENULIS file (perilaku nyata fungsi)
    def _fake_lo(pdf, out):
        import docx as _docx
        _d = _docx.Document()
        _d.add_paragraph("Hasil konversi LibreOffice")
        _d.save(str(out))
        return ""

    monkeypatch.setattr(mod, "_convert_via_libreoffice", _fake_lo)

    pdf = tmp_path / "sumber.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = tmp_path / "out.docx"

    ok, res = convert_pdf_to_docx(pdf, out)
    assert ok is True
    assert res.method == "libreoffice", f"harus memakai LibreOffice, dapat: {res.method}"


def test_convert_pdf_ke_docx_fallback_aman_tanpa_libreoffice(tmp_path: Path, monkeypatch) -> None:
    """PDF invalid → pdf2docx gagal → LibreOffice absen → return failed (bukan crash)."""
    from app.services import pdf_docx_import as mod

    monkeypatch.setattr(mod, "_soffice_bin", lambda: None)
    pdf = tmp_path / "tidak-ada.pdf"
    out = tmp_path / "out.docx"
    ok, res = convert_pdf_to_docx(pdf, out)
    assert ok is False
    assert res.error and "gagal" in res.error.lower() or "libreoffice" in res.error.lower()


def test_post_process_converted_docx_bold_dan_dot_leader(tmp_path: Path) -> None:
    """Refine P1: koreksi bold (tanpa PDF → 0) & normalisasi dot-leader spacing."""
    from app.services.docx_postprocess import post_process_converted_docx

    d = docx.Document()
    d.add_paragraph("Gambar 1. Arsitektur Sistem .............. 12")
    d.add_paragraph("Gambar 2. Diagram Alur ................. 34")
    d.add_paragraph("Ini paragraf biasa.")
    p = d.add_paragraph("Paragraf tebal.")
    for r in p.runs:
        r.font.bold = True
    src = tmp_path / "uji.docx"
    d.save(str(src))

    stats = post_process_converted_docx(src, None)  # tanpa PDF → bold tak berubah
    assert stats["bold_fixed_runs"] == 0
    assert stats["dot_leader_fixed"] >= 2  # dua baris dot-leader dinormalisasi

    d2 = docx.Document(str(src))
    ls = [p.paragraph_format.line_spacing for p in d2.paragraphs if "....." in p.text]
    assert all(x is not None for x in ls), "spacing dot-leader harus diset"


def test_pdf_scan_tanpa_teks_dapat_error_jelas(tmp_path: Path, monkeypatch) -> None:
    """PDF tanpa text layer → pesan error actionable (BUKAN UnboundLocalError)."""
    import fitz

    # buat PDF 1 halaman berisi gambar (tanpa teks)
    pdf = tmp_path / "scan.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # gambar kotak abu-abu sebagai 'scan'
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200), 0)
    pix.clear_with(128)
    page.insert_image(fitz.Rect(50, 50, 250, 250), pixmap=pix)
    doc.save(str(pdf))
    doc.close()

    from app.services import pdf_docx_import as mod

    monkeypatch.setattr(mod, "_soffice_bin", lambda: None)  # nonaktifkan LO
    out = tmp_path / "out.docx"
    ok, res = convert_pdf_to_docx(pdf, out)
    # pdf2docx mungkin tetap menghasilkan DOCX (gambar), tapi ini memastikan
    # tidak ada UnboundLocalError di jalur pemanggil — fokus utama bug P0.
    assert isinstance(ok, bool)
    assert "local variable" not in (res.error or ""), "tidak boleh UnboundLocalError"


def test_upload_dir_abs_anti_cwd(tmp_path: Path, monkeypatch) -> None:
    """upload_dir_abs harus absolut & berbasis proyek (anti-spawn-cwd bug)."""
    from app.core.config import Settings

    s = Settings(UPLOAD_DIR="uploads")
    p = s.upload_dir_abs
    assert p.is_absolute(), "upload_dir_abs harus absolut"
    assert p.name == "uploads"
    # UPLOAD_DIR absolut tetap dihormati
    s2 = Settings(UPLOAD_DIR=str(tmp_path))
    assert s2.upload_dir_abs == tmp_path


def test_get_working_docx_menyajikan_docx_native(tmp_path: Path, monkeypatch) -> None:
    """Endpoint working-docx menyajikan DOCX kerja (bukan markdown)."""
    import uuid

    from app.api.routes.co_writer import get_working_docx
    from starlette.responses import FileResponse

    doc_id = uuid.uuid4()
    # DOCX kerja dibuat di lokasi upload_dir_abs
    from app.core.config import settings

    target = settings.upload_dir_abs / str(doc_id) / "onlyoffice" / "document.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    _buat_docx_uji(target)

    # mock _get_owned_doc + _prepare_onlyoffice_docx supaya tidak butuh DB
    async def _fake_owned(db, doc_id_, user):
        class _D:
            id = doc_id_
            title = "uji"
        return _D()

    monkeypatch.setattr("app.api.routes.co_writer._get_owned_doc", _fake_owned)
    async def _fake_prepare(doc):
        return target

    monkeypatch.setattr("app.api.routes.co_writer._prepare_onlyoffice_docx", _fake_prepare)

    import asyncio

    resp = asyncio.run(get_working_docx(doc_id, object(), None))
    assert isinstance(resp, FileResponse)
    assert Path(resp.path) == target
    assert resp.media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_fix_run_spacing_memperbaiki_kata_nempel(tmp_path: Path) -> None:
    """pdf2docx memecah kata per run tanpa spasi — normalisasi harus menyisipkan.

    Regresi: bug spasi hilang antar-run ("TugasAkhiriniadalah") yang muncul
    di output mentah pdf2docx 0.5.13.
    """
    from app.services.docx_postprocess import _fix_run_spacing

    d = docx.Document()
    p = d.add_paragraph()
    # Simulasikan run pdf2docx: tiap kata run terpisah, tanpa spasi antar-run
    for kata in ["1.", "Tugas", "Akhir", "ini", "adalah", "asli", "dan"]:
        p.add_run(kata)
    # Satu run yang sudah punya spasi trailing (jangan diganggu)
    p2 = d.add_paragraph()
    p2.add_run("NALARAI: ")
    p2.add_run("Pengembangan")
    src = tmp_path / "nempel.docx"
    d.save(str(src))

    n = _fix_run_spacing(src)
    assert n >= 5, f"harus menyisipkan spasi, dapat {n}"

    d2 = docx.Document(str(src))
    t = d2.paragraphs[0].text
    assert t == "1. Tugas Akhir ini adalah asli dan", f"spasi tidak pulih: {t!r}"
    # Paragraf dengan spasi eksisting tidak berubah
    assert d2.paragraphs[1].text == "NALARAI: Pengembangan"


def test_extract_json_array_dari_content_dan_reasoning() -> None:
    """Helper _extract_json_array: parse JSON dari content / reasoning_content.

    Part A harden: kalau content kosong tapi reasoning_content berisi JSON,
    fallback harus bisa mengekstraknya.
    """
    from app.api.routes.co_writer import _extract_json_array

    # content normal
    assert _extract_json_array('[{"level":"serious"}]') == [{"level": "serious"}]
    # content dibungkus ```json ... ```
    assert _extract_json_array('```json\n[{"a":1}]\n```') == [{"a": 1}]
    # content kosong / tanpa array → None (pemicu fallback)
    assert _extract_json_array("") is None
    assert _extract_json_array("maaf, tidak ada masalah ditemukan") is None
    # reasoning_content berisi JSON valid → bisa diekstrak (fallback path)
    assert _extract_json_array('Berikut analisis saya: [{"level":"minor","issue":"typo"}]') == [
        {"level": "minor", "issue": "typo"}
    ]
    # teks sebelum/akhir array tidak menggagalkan ekstraksi (array objek)
    assert _extract_json_array('ok\n[{"a":1},{"b":2}]\nselesai') == [{"a": 1}, {"b": 2}]
    # array angka (mis. "[3]" nomor kandidat literal) BUKAN temuan → None
    assert _extract_json_array('[1,2,3]') is None
    # bukan array → None
    assert _extract_json_array('{"obj": 1}') is None


def test_extract_json_array_tidak_tertipu_bracket_literal() -> None:
    """Bug parsing K3: `[3]` literal di teks kandidat (nomor) tidak boleh
    tertangkap sebagai array JSON. Bracket matching harus seimbang."""
    from app.api.routes.co_writer import _extract_json_array

    # K3: teks kandidat berisi "[3] (Bab 4.1, paragraf 1): ..." + LLM balas array kosong
    raw = (
        '[3] (Bab 4.1, paragraf 1): Berdasarkan Tabel 4.2, hasil pengujian '
        "menunjukkan peningkatan akurasi sebesar 12% dibanding baseline.\n"
        "Validasi: tidak ada masalah nyata, bukti sudah ada.\n[]"
    )
    assert _extract_json_array(raw) == [], (
        f"harus [] (bukan [3]), dapat: {_extract_json_array(raw)}"
    )

    # LLM balas array dengan objek, teks kandidat tetap mengandung [3]
    raw2 = (
        '[3] (Bab 4.1): Berdasarkan Tabel 4.2...\n'
        '[{"location":{"chapter":"4.1","paragraph":1,"anchor_text":"Berdasarkan Tabel 4.2"},'
        '"level":"moderate","issue":"Kalimat ambigu.","suggested_action":"Perjelas.",'
        '"tool_to_call":"insert_or_edit_section"}]'
    )
    hasil = _extract_json_array(raw2)
    assert isinstance(hasil, list) and len(hasil) == 1
    assert hasil[0]["level"] == "moderate"


def test_libreoffice_timeout_mematikan_proses_dan_fallback(tmp_path: Path, monkeypatch) -> None:
    """Part B: bila soffice menggantung melebihi timeout, proses harus di-kill
    (bukan cuma exception) dan convert_pdf_to_docx fallback ke pdf2docx."""
    from app.services import pdf_docx_import as mod

    # 1) Mock Popen yang menggantung: communicate() selalu TimeoutExpired
    class _HangingProc:
        pid = 99999

        def communicate(self, timeout: float):
            raise subprocess.TimeoutExpired(cmd="soffice", timeout=timeout)

        def wait(self, timeout: float | None = None):
            return 0

    import subprocess as _sp

    calls = {"taskkill": 0}

    def _fake_popen(cmd, **kwargs):
        return _HangingProc()

    def _fake_taskkill(args, **kwargs):
        calls["taskkill"] += 1
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(mod.subprocess, "run", _fake_taskkill)

    # 2) pdf2docx fallback harus menghasilkan DOCX valid
    def _fake_pdf2docx(pdf, out):
        import docx as _docx
        _d = _docx.Document()
        _d.add_paragraph("Hasil pdf2docx")
        _d.save(str(out))
        return ""

    monkeypatch.setattr(mod, "_convert_via_pdf2docx", _fake_pdf2docx)
    monkeypatch.setattr(mod, "_soffice_bin", lambda: "soffice")

    pdf = tmp_path / "sumber.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = tmp_path / "out.docx"

    # 3) Panggil fungsi langsung (LibreOffice akan timeout → kill → '' error),
    # lalu pastikan fallback pdf2docx terpicu via convert_pdf_to_docx.
    err = mod._convert_via_libreoffice(pdf, out)
    assert "timeout" in err.lower() or "kill" in err.lower(), f"harus timeout, dapat: {err}"
    assert calls["taskkill"] >= 1, "taskkill harus dipanggil untuk membunuh proses"

    ok, res = mod.convert_pdf_to_docx(pdf, out)
    assert ok is True
    assert res.method == "pdf2docx", f"fallback harus pdf2docx, dapat: {res.method}"


def test_libreoffice_concurrency_profile_terisolasi(tmp_path: Path, monkeypatch) -> None:
    """Part C: tiap panggilan soffice harus pakai UserInstallation unik
    (profile terisolasi) — aman untuk konversi bersamaan."""
    from app.services import pdf_docx_import as mod

    seen_profiles: list[str] = []

    class _FakeProc:
        pid = 12345

        def __init__(self, cmd, **kwargs):
            # Ekstrak -env:UserInstallation=file:///... dari cmd. Bentuk SATU
            # strip (`-env:`) memang yang dipakai kode: LibreOffice 26.2+
            # MENOLAK bentuk dua strip (`--env:`). `startswith("-env:")` tidak
            # keliru cocok dengan `--env:` (char kedua `-` vs `e`).
            for arg in cmd:
                if arg.startswith("-env:UserInstallation="):
                    seen_profiles.append(arg)
            # Tulis file sesuai nama yang dicek _convert_via_libreoffice:
            # out_dir / (pdf.stem + ".docx")
            import docx as _docx
            out_file = Path(cmd[-1]).parent / (Path(cmd[-1]).stem + ".docx")
            _d = _docx.Document()
            _d.add_paragraph("hasil")
            _d.save(str(out_file))

        def communicate(self, timeout: float):
            return ("", "")

        @property
        def returncode(self):
            return 0

        def wait(self, timeout: float | None = None):
            return 0

    monkeypatch.setattr(mod.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(mod, "_soffice_bin", lambda: "soffice")

    pdf = tmp_path / "sumber.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    # Jalankan 3 konversi ke file berbeda — masing-masing harus punya profile unik
    outs = [tmp_path / f"out{i}.docx" for i in range(3)]
    for i, o in enumerate(outs):
        err = mod._convert_via_libreoffice(pdf, o)
        assert err == "", f"konversi {i} gagal: {err}"

    assert len(seen_profiles) == 3, f"harus 3 profile unik, dapat: {len(seen_profiles)}"
    assert len(set(seen_profiles)) == 3, "profile harus unik per panggilan (bukan 1 yang sama)"
