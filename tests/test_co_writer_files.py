"""Uji struktur proyek multi-berkas Co-Writer.

Dua hal yang diuji: pendataran `\\input{}` (yang membuat kompilasi dan analisis
AI melihat naskah utuh, bukan hanya preamble) dan pembersihan jalur berkas
(yang datang dari URL, jadi diperlakukan sebagai masukan tak tepercaya).
"""

from pathlib import Path
from types import SimpleNamespace
import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.co_writer import (
    _ekstrak_outline,
    _perbarui_bibliografi_latex,
    _pecah_per_bab,
    _sumber_preview_proyek,
    _urai_snapshot_proyek,
    auto_mark_document,
    chat_with_document,
    compile_preview,
    create_checkpoint,
    delete_document_file,
    edit_document,
    export_latex,
    get_document_file,
    get_document_image,
    get_document_markdown,
    get_document_outline,
    list_document_files,
    rename_document_file,
    replace_document_term,
    restore_checkpoint,
    save_document_file,
    save_document_from_markdown,
    split_document,
    get_onlyoffice_config,
)
from app.api.routes import co_writer as co_writer_routes
from app.models.co_writer import CoWriterDocument
from app.models.co_writer_checkpoint import CoWriterCheckpoint
from app.models.co_writer_file import JalurTidakSah, bersihkan_jalur
from app.models.co_writer_file import CoWriterFile
from app.models.user import User
from app.schemas.co_writer import CoWriterAutoMarkRequest, CoWriterEditRequest
from app.services import latex_export, pandoc_latex
from app.services.latex_export import (
    compile_latex_pdf,
    datarkan_input,
    localize_latex_image_paths,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_bibliografi_latex_disisipkan_sebelum_end_document_dan_di_escape():
    source = "\\documentclass{article}\n\\begin{document}\nIsi [1].\n\\end{document}\n"

    updated, section = _perbarui_bibliografi_latex(
        source,
        ["[1] A & B, 50% akurat."],
    )

    assert updated.index(section) < updated.index("\\end{document}")
    assert r"{[}1{]} A \& B, 50\% akurat." in section
    assert updated.count("NALAR-AI:BIBLIOGRAPHY:START") == 1


@pytest.mark.anyio
async def test_onlyoffice_config_pdf_impor_menggunakan_dokumen_word_editable(tmp_path, monkeypatch):
    """PDF impor harus dibungkus menjadi DOCX, bukan dibuka sebagai canvas PDF."""
    doc = SimpleNamespace(id=uuid.uuid4(), title="PDF impor")
    user = SimpleNamespace(id=uuid.uuid4(), full_name="Penguji", username="uji@example.com")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-test")
    async def fake_owned(*_args, **_kwargs):
        return doc
    monkeypatch.setattr(co_writer_routes, "_get_owned_doc", fake_owned)
    monkeypatch.setattr(co_writer_routes, "_source_file", lambda _doc_id: (source, "application/pdf"))
    target = tmp_path / "converted.docx"
    async def fake_prepare(_doc):
        target.write_bytes(b"PK-test")
        return target
    monkeypatch.setattr(co_writer_routes, "_prepare_onlyoffice_docx", fake_prepare)
    result = await get_onlyoffice_config(doc.id, current_user=user, db=None)
    assert result["config"]["document"]["fileType"] == "docx"
    assert result["config"]["documentType"] == "word"


@pytest.mark.anyio
async def test_edit_ai_menerima_schema_pydantic_tanpa_menganggapnya_dict(monkeypatch):
    async def resolve_palsu(*args, **kwargs):
        return SimpleNamespace()

    class WriterPalsu:
        def __init__(self, llm):
            self.llm = llm

        async def ask(self, prompt, *, system, **kwargs):
            return r"\section{Pendahuluan}" + "\nKalimat akademik."

    monkeypatch.setattr(co_writer_routes, "_resolve_llm", resolve_palsu)
    monkeypatch.setattr(co_writer_routes, "CoWriterLLM", WriterPalsu)

    hasil = await edit_document(
        CoWriterEditRequest(
            text="Kalimat awal.",
            instruction="Perbaiki gaya akademik.",
            action="rewrite",
        ),
        SimpleNamespace(id=uuid.uuid4()),
        None,
    )

    assert "Kalimat akademik" in hasil.edited_text


@pytest.mark.anyio
async def test_edit_ai_timeout_menjadi_504(monkeypatch):
    async def resolve_palsu(*args, **kwargs):
        return SimpleNamespace()

    class WriterLambat:
        def __init__(self, llm):
            self.llm = llm

        async def ask(self, *args, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(co_writer_routes, "_resolve_llm", resolve_palsu)
    monkeypatch.setattr(co_writer_routes, "CoWriterLLM", WriterLambat)
    monkeypatch.setattr(co_writer_routes, "CO_WRITER_REQUEST_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(HTTPException) as exc_info:
        await edit_document(
            CoWriterEditRequest(text="Draf.", instruction="Perbaiki."),
            SimpleNamespace(id=uuid.uuid4()),
            None,
        )

    assert exc_info.value.status_code == 504
    assert "tidak merespons" in exc_info.value.detail


@pytest.mark.anyio
async def test_automark_ai_error_provider_menjadi_502(monkeypatch):
    async def resolve_palsu(*args, **kwargs):
        return SimpleNamespace()

    class WriterGagal:
        def __init__(self, llm):
            self.llm = llm

        async def ask(self, *args, **kwargs):
            raise RuntimeError("provider terputus")

    monkeypatch.setattr(co_writer_routes, "_resolve_llm", resolve_palsu)
    monkeypatch.setattr(co_writer_routes, "CoWriterLLM", WriterGagal)

    with pytest.raises(HTTPException) as exc_info:
        await auto_mark_document(
            CoWriterAutoMarkRequest(text="Judul\nIsi"),
            SimpleNamespace(id=uuid.uuid4()),
            None,
        )

    assert exc_info.value.status_code == 502
    assert "provider terputus" in exc_info.value.detail


def test_bibliografi_latex_diganti_tanpa_duplikasi():
    source = "\\documentclass{article}\n\\begin{document}\nIsi.\n\\end{document}\n"
    first, _ = _perbarui_bibliografi_latex(source, ["[1] Lama"])

    updated, section = _perbarui_bibliografi_latex(first, ["[2] Baru"])

    assert "Lama" not in updated
    assert "Baru" in section
    assert updated.count("NALAR-AI:BIBLIOGRAPHY:START") == 1
    assert updated.count("\\section*{Daftar Pustaka}") == 1


async def _db_proyek_sementara(tmp_path, content: str):
    url = URL.create("sqlite+aiosqlite", database=str(tmp_path / "co-writer.db"))
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda koneksi: User.metadata.create_all(
                koneksi,
                tables=[
                    User.__table__,
                    CoWriterDocument.__table__,
                    CoWriterCheckpoint.__table__,
                    CoWriterFile.__table__,
                ],
            )
        )
    pembuat_sesi = async_sessionmaker(engine, expire_on_commit=False)
    sesi = pembuat_sesi()
    user = User(username="penguji", hashed_password="x")
    sesi.add(user)
    await sesi.flush()
    doc = CoWriterDocument(
        user_id=user.id,
        title="Uji",
        content=content,
        content_format="latex",
    )
    sesi.add(doc)
    await sesi.commit()
    await sesi.refresh(doc)
    return engine, sesi, user, doc


def test_datarkan_input_dasar():
    main = "\n".join([
        r"\begin{document}",
        r"\input{bab/01-pendahuluan.tex}",
        r"\end{document}",
    ])
    hasil = datarkan_input(main, {"bab/01-pendahuluan.tex": r"\section{Pendahuluan}"})
    assert r"\section{Pendahuluan}" in hasil
    assert r"\input{" not in hasil


def test_ekstrak_outline_latex_dengan_ringkasan_dan_lokasi():
    hasil = _ekstrak_outline(
        {
            "main.tex": r"\documentclass{article}\n\input{bab/01.tex}",
            "bab/01.tex": "\n".join(
                [
                    r"\section{Pendahuluan}",
                    "Penelitian ini membahas sistem penulisan akademik terpadu.",
                    r"\subsection{Tujuan}",
                    "Tujuan penelitian adalah mempercepat penyusunan tesis.",
                ]
            ),
        }
    )
    assert [(item["path"], item["level"], item["title"]) for item in hasil] == [
        ("bab/01.tex", 1, "Pendahuluan"),
        ("bab/01.tex", 2, "Tujuan"),
    ]
    assert hasil[0]["offset"] == 0
    assert hasil[0]["word_count"] >= 7
    assert "sistem penulisan" in hasil[0]["summary"]


def test_ekstrak_outline_markdown_lama_tetap_didukung():
    hasil = _ekstrak_outline({"main.tex": "# Bab Satu\nIsi bab.\n## Bagian\nIsi."})
    assert [(item["level"], item["title"]) for item in hasil] == [
        (1, "Bab Satu"),
        (2, "Bagian"),
    ]


def test_checkpoint_lama_tetap_dibaca_sebagai_main_tex():
    main, files = _urai_snapshot_proyek("isi checkpoint lama")
    assert main == "isi checkpoint lama"
    assert files is None


@pytest.mark.anyio
async def test_chat_co_writer_menolak_format_gambar_tidak_aman():
    with pytest.raises(HTTPException) as exc_info:
        await chat_with_document(
            uuid.uuid4(),
            {"message": "Baca gambar", "images": ["https://example.com/a.png"]},
            None,
            None,
        )
    assert exc_info.value.status_code == 422
    assert "PNG/JPEG" in exc_info.value.detail


def test_compile_latex_pdf_memakai_decode_utf8_yang_tahan_error(tmp_path, monkeypatch):
    panggilan = {}

    def subprocess_palsu(*args, **kwargs):
        panggilan["command"] = args[0]
        panggilan.update(kwargs)
        return SimpleNamespace(returncode=1, stderr="byte rusak: \ufffd", stdout="")

    monkeypatch.setattr(latex_export.subprocess, "run", subprocess_palsu)

    with pytest.raises(RuntimeError, match="byte rusak: \ufffd"):
        compile_latex_pdf(r"\documentclass{article}", str(tmp_path))

    assert panggilan["encoding"] == "utf-8"
    assert panggilan["errors"] == "replace"
    assert panggilan["command"][-2:] == ["-Z", "continue-on-errors"]


def test_compile_latex_pdf_stderr_none_memakai_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        latex_export.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stderr=None,
            stdout="galat dari stdout",
        ),
    )

    with pytest.raises(RuntimeError, match="galat dari stdout"):
        compile_latex_pdf(r"\documentclass{article}", str(tmp_path))


def test_compile_latex_pdf_menormalkan_ligatur_pdf(tmp_path, monkeypatch):
    def subprocess_palsu(command, **kwargs):
        tex_path = Path(command[1])
        assert "efisien dan fleksibel" in tex_path.read_text(encoding="utf-8")
        (tmp_path / "laporan.pdf").write_bytes(b"%PDF-uji")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(latex_export.subprocess, "run", subprocess_palsu)

    hasil = compile_latex_pdf("e\ufb01sien dan \ufb02eksibel", str(tmp_path))
    assert hasil == str(tmp_path / "laporan.pdf")


def test_datarkan_input_tanpa_ekstensi():
    """LaTeX membolehkan `\\input{bab/01-x}`; berkasnya tetap `bab/01-x.tex`."""
    hasil = datarkan_input(r"\input{bab/01-x}", {"bab/01-x.tex": "isi bab satu"})
    assert hasil == "isi bab satu"


def test_datarkan_input_nama_dasar_saja():
    hasil = datarkan_input(r"\input{01-x}", {"bab/01-x.tex": "isi bab satu"})
    assert hasil == "isi bab satu"


def test_datarkan_input_nama_dasar_ambigu_dibiarkan():
    """Dua berkas bernama sama di folder berbeda → jangan menebak."""
    berkas = {"bab/01-x.tex": "A", "lampiran/01-x.tex": "B"}
    hasil = datarkan_input(r"\input{01-x}", berkas)
    assert hasil == r"\input{01-x}"


def test_datarkan_input_bersarang():
    berkas = {
        "bab/01.tex": "awal\n" + r"\input{bab/01a.tex}" + "\nakhir",
        "bab/01a.tex": "isi bersarang",
    }
    hasil = datarkan_input(r"\input{bab/01.tex}", berkas)
    assert "isi bersarang" in hasil
    assert "awal" in hasil and "akhir" in hasil


def test_datarkan_input_melingkar_berhenti():
    """Dua berkas yang saling meng-input harus berhenti, bukan menggantung."""
    berkas = {"a.tex": r"\input{b.tex}", "b.tex": r"\input{a.tex}"}
    hasil = datarkan_input(r"\input{a.tex}", berkas)
    # Yang penting: selesai dan menyisakan baris \input agar galatnya terlihat
    # di log kompilasi, bukan rekursi tak berujung.
    assert r"\input{" in hasil


def test_datarkan_input_berkas_hilang_dibiarkan():
    hasil = datarkan_input(r"\input{bab/tidak-ada.tex}", {"bab/lain.tex": "x"})
    assert hasil == r"\input{bab/tidak-ada.tex}"


def test_datarkan_input_include_juga():
    hasil = datarkan_input(r"\include{bab/01.tex}", {"bab/01.tex": "isi"})
    assert hasil == "isi"


def test_datarkan_input_tanpa_berkas_anak():
    """Draf berkas tunggal lewat tanpa perubahan sama sekali."""
    sumber = r"\section{Satu}" + "\nisi biasa"
    assert datarkan_input(sumber, {}) == sumber


def test_bersihkan_jalur_normal():
    assert bersihkan_jalur("bab/01-pendahuluan.tex") == "bab/01-pendahuluan.tex"
    assert bersihkan_jalur("./bab//01.tex") == "bab/01.tex"
    assert bersihkan_jalur("bab\\01.tex") == "bab/01.tex"


@pytest.mark.parametrize(
    "jalur",
    ["../rahasia.tex", "bab/../../keluar.tex", "/etc/passwd", "C:/Windows/x.tex", "", "   "],
)
def test_bersihkan_jalur_tolak(jalur):
    with pytest.raises(JalurTidakSah):
        bersihkan_jalur(jalur)


def test_preview_menyuntikkan_berkas_aktif_ke_proyek_utuh():
    utama = "\n".join([
        r"\documentclass{article}",
        r"\begin{document}",
        r"\input{bab/01-awal.tex}",
        r"\end{document}",
    ])
    hasil = _sumber_preview_proyek(
        utama,
        {"bab/01-awal.tex": r"\section{Versi tersimpan}"},
        isi_editor=r"\section{Versi editor}",
        jalur_editor="bab/01-awal.tex",
        isi_diberikan=True,
    )
    assert r"\documentclass{article}" in hasil
    assert r"\section{Versi editor}" in hasil
    assert "Versi tersimpan" not in hasil


def test_preview_main_tex_dan_buffer_kosong_tetap_dihormati():
    assert _sumber_preview_proyek(
        "tersimpan",
        {},
        isi_editor="",
        jalur_editor="main.tex",
        isi_diberikan=True,
    ) == ""


def test_preview_tanpa_content_memakai_proyek_tersimpan():
    hasil = _sumber_preview_proyek(
        r"\input{bab/01.tex}",
        {"bab/01.tex": "isi tersimpan"},
    )
    assert hasil == "isi tersimpan"


@pytest.mark.anyio
async def test_compile_preview_gagal_tectonic_menjadi_422(tmp_path, monkeypatch):
    engine, db, user, doc = await _db_proyek_sementara(
        tmp_path,
        r"\documentclass{article}\begin{document}uji\end{document}",
    )

    def compile_gagal(*args, **kwargs):
        raise RuntimeError("Tectonic gagal:\nlog kompilasi")

    monkeypatch.setattr(co_writer_routes, "compile_latex_pdf", compile_gagal)
    try:
        with pytest.raises(HTTPException) as exc_info:
            await compile_preview(doc.id, {}, user, db)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["message"] == "Kompilasi LaTeX gagal."
        assert "log kompilasi" in exc_info.value.detail["log"]
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_export_pdf_memuat_isi_semua_berkas_proyek(tmp_path, monkeypatch):
    utama = "\n".join(
        [
            r"\documentclass{article}",
            r"\begin{document}",
            r"\input{bab/01-pendahuluan.tex}",
            r"\end{document}",
        ]
    )
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, utama)
    try:
        db.add(
            CoWriterFile(
                doc_id=doc.id,
                user_id=user.id,
                path="bab/01-pendahuluan.tex",
                content=r"\section*{Pendahuluan}" + "\nIsi bab lengkap.",
            )
        )
        await db.commit()
        panggilan = {}

        # Jalur utama mengkompilasi .tex LANGSUNG, sumber yang sama dengan
        # /compile. Yang direkam karena itu kode LaTeX, bukan markdown.
        def tectonic_palsu(sumber, output_dir, jobname, *args, **kwargs):
            panggilan["tex"] = sumber
            hasil = Path(output_dir) / f"{jobname}.pdf"
            hasil.write_bytes(b"%PDF-uji")
            return str(hasil)

        monkeypatch.setattr(co_writer_routes, "compile_latex_pdf", tectonic_palsu)

        response = await export_latex(doc.id, "pdf", user, db)

        assert response.media_type == "application/pdf"
        assert r"\section*{Pendahuluan}" in panggilan["tex"]
        assert "Isi bab lengkap." in panggilan["tex"]
        assert r"\input{" not in panggilan["tex"]
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_export_pdf_tidak_lewat_markdown_jadi_section_bintang_utuh(
    tmp_path, monkeypatch
):
    """Judul bertanda bintang harus tiba di kompiler dengan bintangnya utuh.

    Ini uji regresi laporan "hasil ekspor tidak sama dengan laporan asli":
    jalur lama memutar naskah lewat `latex_to_markdown`, yang memetakan
    `\\section*{...}` dan `\\section{...}` ke `# ...` yang sama. Pandoc lalu
    menomori ulang setiap judul, sehingga laporan yang aslinya tak bernomor
    keluar dengan nomor — dan `\\newpage` ikut hilang. Jadi yang diperiksa
    bukan "ekspor menghasilkan PDF" melainkan bahwa Pandoc tidak dipanggil.
    """
    utama = "\n".join(
        [
            r"\documentclass{article}",
            r"\begin{document}",
            r"\section*{Bab I Pendahuluan}",
            r"\newpage",
            r"\section*{Bab II Tinjauan}",
            r"\end{document}",
        ]
    )
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, utama)
    try:

        def tectonic_palsu(sumber, output_dir, jobname, *args, **kwargs):
            hasil = Path(output_dir) / f"{jobname}.pdf"
            hasil.write_bytes(b"%PDF-uji")
            return str(hasil)

        def pandoc_terlarang(*args, **kwargs):
            raise AssertionError("Pandoc tidak boleh dipakai saat tectonic berhasil.")

        monkeypatch.setattr(co_writer_routes, "compile_latex_pdf", tectonic_palsu)
        monkeypatch.setattr(pandoc_latex, "pandoc_to_pdf", pandoc_terlarang)

        response = await export_latex(doc.id, "pdf", user, db)

        assert response.media_type == "application/pdf"
        # Tanpa notifikasi cadangan: jalur utama yang dipakai, bukan cadangan.
        assert "X-Fallback-Notice" not in (response.headers or {})
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_export_pdf_gagal_tectonic_jatuh_ke_pandoc_dengan_notifikasi(
    tmp_path, monkeypatch
):
    """Kompilasi gagal tetap menghasilkan berkas, tapi harus diberi tahu.

    PDF hasil Pandoc penomorannya bisa berbeda dari pratinjau; mengirimkannya
    tanpa keterangan membuat selisih itu tampak seperti cacat baru.
    """
    utama = "\n".join(
        [
            r"\documentclass{article}",
            r"\begin{document}",
            r"\section*{Pendahuluan}",
            r"\end{document}",
        ]
    )
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, utama)
    try:

        def tectonic_gagal(*args, **kwargs):
            raise RuntimeError("! LaTeX Error: File `x.sty' not found.")

        def pandoc_palsu(markdown, output_path, **kwargs):
            Path(output_path).write_bytes(b"%PDF-uji")
            return output_path

        monkeypatch.setattr(co_writer_routes, "compile_latex_pdf", tectonic_gagal)
        monkeypatch.setattr(pandoc_latex, "pandoc_to_pdf", pandoc_palsu)
        monkeypatch.setattr(pandoc_latex, "_tersedia", lambda: True)

        response = await export_latex(doc.id, "pdf", user, db)

        assert response.media_type == "application/pdf"
        notifikasi = response.headers["x-fallback-notice"]
        assert "Pandoc" in notifikasi
        assert notifikasi.isascii()
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_outline_endpoint_membaca_semua_berkas_proyek(tmp_path):
    engine, db, user, doc = await _db_proyek_sementara(
        tmp_path,
        "\n".join(
            [
                r"\documentclass{article}",
                r"\begin{document}",
                r"\input{bab/01.tex}",
                r"\end{document}",
            ]
        ),
    )
    try:
        await save_document_file(
            doc.id,
            "bab/01.tex",
            {"content": r"\section{Pendahuluan}" + "\nIsi pendahuluan."},
            user,
            db,
        )
        hasil = await get_document_outline(doc.id, user, db)
        assert hasil["total"] == 1
        assert hasil["headings"][0]["path"] == "bab/01.tex"
        assert hasil["headings"][0]["title"] == "Pendahuluan"
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_checkpoint_memulihkan_seluruh_proyek_multiberkas(tmp_path):
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, "main versi satu")
    try:
        await save_document_file(doc.id, "bab/01.tex", {"content": "bab satu"}, user, db)
        checkpoint = await create_checkpoint(
            doc.id,
            {"label": "Sebelum perubahan besar"},
            user,
            db,
        )

        doc.content = "main versi dua"
        await db.commit()
        await save_document_file(doc.id, "bab/01.tex", {"content": "bab berubah"}, user, db)
        await save_document_file(doc.id, "bab/02.tex", {"content": "bab baru"}, user, db)

        await restore_checkpoint(doc.id, uuid.UUID(checkpoint["id"]), user, db)
        await db.refresh(doc)
        files = await list_document_files(doc.id, user, db)
        assert doc.content == "main versi satu"
        assert [item["path"] for item in files["files"]] == ["bab/01.tex"]
        restored = await get_document_file(doc.id, "bab/01.tex", user, db)
        assert restored["content"] == "bab satu"

        count = await db.scalar(
            select(func.count()).select_from(CoWriterCheckpoint).where(
                CoWriterCheckpoint.doc_id == doc.id
            )
        )
        assert count == 2
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_seragamkan_istilah_mengubah_seluruh_proyek_dengan_checkpoint(tmp_path):
    engine, db, user, doc = await _db_proyek_sementara(
        tmp_path,
        "NalarAI membantu penulis.",
    )
    try:
        await save_document_file(
            doc.id,
            "bab/01.tex",
            {"content": "Evaluasi NalarAI dilakukan."},
            user,
            db,
        )
        hasil = await replace_document_term(
            doc.id,
            {"from": "NalarAI", "to": "Nalar AI"},
            user,
            db,
        )
        await db.refresh(doc)
        child = await get_document_file(doc.id, "bab/01.tex", user, db)
        assert hasil == {"replaced": 2, "files_changed": 1}
        assert doc.content == "Nalar AI membantu penulis."
        assert child["content"] == "Evaluasi Nalar AI dilakukan."
        checkpoint = await db.scalar(
            select(CoWriterCheckpoint).where(CoWriterCheckpoint.doc_id == doc.id)
        )
        assert checkpoint is not None
    finally:
        await db.close()
        await engine.dispose()


def test_pecah_per_bab_menyisakan_preamble_dan_end_document():
    sumber = "\n".join([
        r"\documentclass{article}",
        r"\begin{document}",
        r"\maketitle",
        r"\section{Pendahuluan}",
        "Isi pertama.",
        r"\subsection{Latar Belakang}",
        "Detail.",
        r"\section{Metode Penelitian}",
        "Isi kedua.",
        r"\end{document}",
    ])
    utama, bab = _pecah_per_bab(sumber)

    assert r"\documentclass{article}" in utama
    assert r"\maketitle" in utama
    assert r"\input{bab/01-pendahuluan.tex}" in utama
    assert r"\input{bab/02-metode-penelitian.tex}" in utama
    assert utama.rstrip().endswith(r"\end{document}")
    assert [jalur for jalur, _ in bab] == [
        "bab/01-pendahuluan.tex",
        "bab/02-metode-penelitian.tex",
    ]
    assert r"\subsection{Latar Belakang}" in bab[0][1]
    assert r"\end{document}" not in bab[-1][1]


def test_pecah_per_bab_slug_unicode_dan_jumlah_tiga_digit():
    sections = "\n".join(
        rf"\section{{Bab ke-{i}: Evaluasi Akurasi}}\nIsi {i}"
        for i in range(1, 101)
    )
    _, bab = _pecah_per_bab(sections)
    assert bab[0][0] == "bab/01-bab-ke-1-evaluasi-akurasi.tex"
    assert bab[-1][0] == "bab/100-bab-ke-100-evaluasi-akurasi.tex"


def test_pecah_per_bab_mengenali_chapter_dan_menyimpan_section_di_dalamnya():
    sumber = "\n".join(
        [
            r"\documentclass{report}",
            r"\begin{document}",
            r"\chapter{Pendahuluan}",
            r"\section{Latar Belakang}",
            "Isi pendahuluan.",
            r"\chapter*{Metodologi}",
            "Isi metodologi.",
            r"\end{document}",
        ]
    )

    utama, bab = _pecah_per_bab(sumber)

    assert r"\input{bab/01-pendahuluan.tex}" in utama
    assert r"\input{bab/02-metodologi.tex}" in utama
    assert r"\section{Latar Belakang}" in bab[0][1]
    assert r"\end{document}" not in bab[-1][1]


def test_localize_latex_image_paths_memetakan_url_dan_jalur_relatif(tmp_path):
    gambar = tmp_path / "logo.png"
    gambar.write_bytes(b"png")
    source = r"\includegraphics{gambar/logo.png}" + "\n" + r"\includegraphics{https://example.com/missing.png}"

    hasil = localize_latex_image_paths(source, [str(tmp_path)])

    assert str(gambar).replace("\\", "/") in hasil
    assert r"https://example.com/missing.png" in hasil


def test_pecah_per_bab_menolak_dokumen_tanpa_section():
    with pytest.raises(ValueError, match="tidak memiliki"):
        _pecah_per_bab(r"\begin{document}Teks saja\end{document}")


@pytest.mark.anyio
async def test_crud_berkas_dalam_transaksi_sqlite(tmp_path):
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, "main")
    try:
        tersimpan = await save_document_file(
            doc.id,
            "bab/01-pendahuluan.tex",
            {"content": "isi bab"},
            user,
            db,
        )
        assert tersimpan["path"] == "bab/01-pendahuluan.tex"

        daftar = await list_document_files(doc.id, user, db)
        assert daftar["files"] == [
            {
                "path": "bab/01-pendahuluan.tex",
                "size": len("isi bab".encode("utf-8")),
                "updated_at": tersimpan["updated_at"],
            }
        ]

        satu = await get_document_file(
            doc.id, "bab/01-pendahuluan.tex", user, db
        )
        assert satu["content"] == "isi bab"

        diganti = await rename_document_file(
            doc.id,
            {"from": "bab/01-pendahuluan.tex", "to": "bab/01-baru.tex"},
            user,
            db,
        )
        assert diganti["path"] == "bab/01-baru.tex"
        with pytest.raises(HTTPException) as lama:
            await get_document_file(
                doc.id, "bab/01-pendahuluan.tex", user, db
            )
        assert lama.value.status_code == 404

        dihapus = await delete_document_file(doc.id, "bab/01-baru.tex", user, db)
        assert dihapus == {"deleted": True, "path": "bab/01-baru.tex"}
        assert (await list_document_files(doc.id, user, db))["files"] == []
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_hapus_dokumen_ikut_menghapus_berkas_dan_checkpoint(tmp_path, monkeypatch):
    """SQLite mengabaikan ON DELETE CASCADE kecuali PRAGMA foreign_keys=ON.

    Karena pragma itu tidak disetel, menghapus draf pernah meninggalkan baris
    `co_writer_files` dan `co_writer_checkpoints` sebagai yatim — tumbuh terus
    tiap kali pengguna menghapus draf.
    """
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, "main")
    monkeypatch.setattr(
        co_writer_routes, "_document_upload_dir", lambda _doc_id: tmp_path / "uploads"
    )
    try:
        await save_document_file(doc.id, "bab/01.tex", {"content": "isi"}, user, db)
        await create_checkpoint(doc.id, {"label": "uji"}, user, db)
        assert (
            await db.scalar(
                select(func.count()).select_from(CoWriterFile).where(CoWriterFile.doc_id == doc.id)
            )
        ) == 1

        await co_writer_routes.delete_document(doc.id, user, db)

        sisa_berkas = await db.scalar(
            select(func.count()).select_from(CoWriterFile).where(CoWriterFile.doc_id == doc.id)
        )
        sisa_checkpoint = await db.scalar(
            select(func.count())
            .select_from(CoWriterCheckpoint)
            .where(CoWriterCheckpoint.doc_id == doc.id)
        )
        assert sisa_berkas == 0
        assert sisa_checkpoint == 0
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_split_membuat_checkpoint_dan_berkas_bab_atomik(tmp_path):
    sumber = "\n".join([
        r"\documentclass{article}",
        r"\begin{document}",
        r"\section{Satu}",
        "Isi satu.",
        r"\section{Dua}",
        "Isi dua.",
        r"\end{document}",
    ])
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, sumber)
    try:
        hasil = await split_document(doc.id, user, db)
        assert [item["path"] for item in hasil["files"]] == [
            "bab/01-satu.tex",
            "bab/02-dua.tex",
        ]
        assert r"\input{bab/01-satu.tex}" in hasil["content"]

        checkpoint = await db.scalar(
            select(CoWriterCheckpoint).where(CoWriterCheckpoint.doc_id == doc.id)
        )
        assert checkpoint is not None
        checkpoint_main, checkpoint_files = _urai_snapshot_proyek(
            checkpoint.content_snapshot
        )
        assert checkpoint_main == sumber
        assert checkpoint_files == {}
        assert str(checkpoint.id) == hasil["checkpoint_id"]

        jumlah = await db.scalar(
            select(func.count()).select_from(CoWriterFile).where(CoWriterFile.doc_id == doc.id)
        )
        assert jumlah == 2

        with pytest.raises(HTTPException) as ulang:
            await split_document(doc.id, user, db)
        assert ulang.value.status_code == 409
        jumlah_checkpoint = await db.scalar(
            select(func.count())
            .select_from(CoWriterCheckpoint)
            .where(CoWriterCheckpoint.doc_id == doc.id)
        )
        assert jumlah_checkpoint == 1
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_daftar_gambar_baca_saja_dan_bisa_dibuka(tmp_path, monkeypatch):
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, "main")
    try:
        monkeypatch.chdir(tmp_path)
        images = tmp_path / "uploads" / str(doc.id) / "images"
        images.mkdir(parents=True)
        gambar = images / "diagram satu.png"
        gambar.write_bytes(b"PNG")

        daftar = await list_document_files(doc.id, user, db)
        assert daftar["files"] == [
            {
                "path": "gambar/diagram satu.png",
                "size": 3,
                "updated_at": int(gambar.stat().st_mtime),
                "read_only": True,
                "url": (
                    f"/api/v1/co_writer/documents/{doc.id}/images/"
                    "diagram%20satu.png"
                ),
            }
        ]

        respons = await get_document_image(
            doc.id, "diagram satu.png", user, db
        )
        assert Path(respons.path).resolve() == gambar.resolve()
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_md_roundtrip_tidak_mengubah_judul(tmp_path):
    """Mode edit ala Word: LaTeX → Markdown → LaTeX, judul TA wajib tetap."""
    latex_awal = (
        r"\documentclass{article}" + "\n"
        + r"\begin{document}" + "\n"
        + r"\section{Pendahuluan}" + "\n"
        + "Kalimat pembuka pertama.\n"
        + r"\end{document}" + "\n"
    )
    # Raw-string memakai satu garis miring: verifikasi bahwa benar-benar LaTeX.
    assert latex_awal.startswith("\\documentclass")
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, latex_awal)
    try:
        md = await get_document_markdown(doc.id, current_user=user, db=db)
        assert "Pendahuluan" in md["markdown"]
        assert md["title"] == "Uji"

        disunting = md["markdown"].replace(
            "Kalimat pembuka pertama.", "Kalimat pembuka yang sudah disunting."
        )
        hasil = await save_document_from_markdown(
            doc.id, {"markdown": disunting}, current_user=user, db=db
        )
        assert "Pendahuluan" in hasil["content"]
        assert "disunting" in hasil["content"]
        # Judul TA tidak boleh berubah walau isi diubah.
        assert hasil["title"] == "Uji"

        ulang = await get_document_markdown(doc.id, current_user=user, db=db)
        assert "disunting" in ulang["markdown"]
        assert "Pendahuluan" in ulang["markdown"]
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_from_md_menyimpan_ke_berkas_anak_tanpa_menyentuh_main(tmp_path):
    engine, db, user, doc = await _db_proyek_sementara(tmp_path, "main asli")
    try:
        hasil = await save_document_from_markdown(
            doc.id,
            {"markdown": "## Pendahuluan\nIsi bab.", "path": "bab/01-pendahuluan.tex"},
            current_user=user,
            db=db,
        )
        assert hasil["path"] == "bab/01-pendahuluan.tex"
        assert "Pendahuluan" in hasil["content"]

        # main.tex tidak tersentuh oleh simpanan berkas anak.
        satu = await get_document_file(doc.id, "bab/01-pendahuluan.tex", user, db)
        assert satu["content"] == hasil["content"]

        doc2 = await get_document_markdown(doc.id, current_user=user, db=db)
        assert "main asli" in doc2["markdown"]
    finally:
        await db.close()
        await engine.dispose()
