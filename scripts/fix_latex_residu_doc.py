"""Bersihkan LaTeX residu pada satu dokumen co-writer → Markdown murni.

Sekali jalan (one-off). Aman & bisa dipulihkan:
- Snapshot isi LaTeX asli disimpan sebagai CoWriterCheckpoint via simpan_checkpoint.
- content_format TETAP 'markdown' (tak berubah), hanya doc.content dibersihkan.
- Idempoten: kalau isi sudah bersih (tak ada perintah LaTeX), tidak menulis apa pun.

Latar: doc ini ber-content_format='markdown' TAPI isinya 100% LaTeX, sehingga
migrasi otomatis di get_document (yang hanya jalan bila content_format != 'markdown')
melewatinya. Skrip ini menerapkan konverter latex_to_markdown yang sudah diperbaiki.
"""

import asyncio
import re
import uuid

from app.api.routes.co_writer import simpan_checkpoint
from app.db.session import AsyncSessionLocal
from app.models.co_writer import CoWriterDocument
from app.models.user import User
from app.services.latex_export import latex_to_markdown

DOC_ID_HEX = "9cc6fe07d2be4480be356adafb01b9a1"
_LATEX_PROBE = re.compile(
    r"\\(?:title|maketitle|author|date|section|subsection|begin|end|textbf|documentclass)\b"
)


async def main() -> None:
    doc_pk = uuid.UUID(DOC_ID_HEX)
    async with AsyncSessionLocal() as db:
        doc = await db.get(CoWriterDocument, doc_pk)
        if doc is None:
            print(f"[GAGAL] Dokumen {DOC_ID_HEX} tidak ditemukan.")
            return

        asli = doc.content or ""
        print(f"[INFO] Ditemukan. content_format={doc.content_format!r} panjang_asli={len(asli)}")

        if not _LATEX_PROBE.search(asli):
            print("[SKIP] Isi sudah tidak mengandung perintah LaTeX. Tidak ada yang diubah.")
            return

        user = await db.get(User, doc.user_id)
        if user is None:
            print(f"[GAGAL] User {doc.user_id!r} tidak ditemukan; batal (butuh user utk checkpoint).")
            return

        # 1) Backup isi LaTeX asli ke checkpoint (belum commit).
        cp = await simpan_checkpoint(
            db, doc, user, "Sebelum bersihkan LaTeX residu (markdown-first)"
        )
        print(f"[OK] Checkpoint dibuat: label={cp.label!r}")

        # 2) Konversi ke Markdown murni.
        bersih = latex_to_markdown(asli)
        sisa_backslash = bersih.count("\\")
        print(f"[INFO] panjang_baru={len(bersih)} sisa_backslash={sisa_backslash}")
        if _LATEX_PROBE.search(bersih):
            print("[BATAL] Hasil konversi MASIH mengandung perintah LaTeX — rollback, tidak commit.")
            await db.rollback()
            return

        doc.content = bersih
        # content_format sengaja TIDAK diubah (tetap 'markdown').

        # 3) Commit (checkpoint + doc sekaligus).
        await db.commit()
        print("[SUKSES] Doc dibersihkan & disimpan. LaTeX asli aman di checkpoint.")

        # 4) Verifikasi ulang dari DB.
        await db.refresh(doc)
        cek = doc.content or ""
        print(
            f"[VERIFIKASI] panjang_tersimpan={len(cek)} "
            f"masih_ada_latex={bool(_LATEX_PROBE.search(cek))} "
            f"cuplikan_awal={cek[:80]!r}"
        )


if __name__ == "__main__":
    asyncio.run(main())
