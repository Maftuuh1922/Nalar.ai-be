"""Berkas anak sebuah proyek Co-Writer (struktur folder ala Overleaf).

Satu draf tidak lagi harus berupa satu blob teks: `co_writer_documents.content`
tetap menjadi `main.tex` (preamble + deret `\\input{}`), sedangkan tiap bab dan
`references.bib` menjadi baris di tabel ini. Gambar TIDAK disimpan di sini —
kolom `content` khusus teks (`.tex` / `.bib`); berkas gambar tetap berada di
`uploads/{doc_id}/images/` dan hanya ditampilkan di pohon berkas.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CoWriterFile(Base):
    __tablename__ = "co_writer_files"
    # Dua berkas dengan jalur sama dalam satu proyek adalah keadaan tak sah:
    # `\input{bab/01-x}` jadi ambigu. Basis data tempat yang benar untuk
    # menegakkannya, bukan pemeriksaan di rute yang bisa balapan.
    __table_args__ = (UniqueConstraint("doc_id", "path", name="uq_co_writer_files_doc_path"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    doc_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("co_writer_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class JalurTidakSah(ValueError):
    """Jalur berkas menunjuk ke luar proyek atau tidak bisa dinormalkan."""


def bersihkan_jalur(jalur: str) -> str:
    """Normalkan jalur relatif proyek; tolak yang keluar dari akar proyek.

    Jalur berkas datang dari URL, jadi diperlakukan sebagai masukan tak
    tepercaya: `..` bisa menabrak berkas proyek lain saat nanti dimaterialisasi
    ke direktori kompilasi, dan jalur absolut bisa menunjuk ke mana saja di
    disk. Pemisah `\\` disamakan jadi `/` supaya `bab\\01.tex` dari klien
    Windows tidak menghasilkan berkas kedua yang berbeda dari `bab/01.tex`.
    """
    mentah = (jalur or "").replace("\\", "/").strip()
    if not mentah:
        raise JalurTidakSah("jalur kosong")
    if mentah.startswith("/") or (len(mentah) > 1 and mentah[1] == ":"):
        raise JalurTidakSah(f"jalur absolut tidak diizinkan: {jalur!r}")

    bagian: list[str] = []
    for potong in mentah.split("/"):
        if potong in ("", "."):
            continue
        if potong == "..":
            raise JalurTidakSah(f"jalur keluar dari proyek: {jalur!r}")
        bagian.append(potong)

    if not bagian:
        raise JalurTidakSah("jalur kosong")
    bersih = "/".join(bagian)
    if len(bersih) > 512:
        raise JalurTidakSah("jalur terlalu panjang")
    return bersih
