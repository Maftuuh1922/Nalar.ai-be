"""Diagnosa impor PDF pengguna: jalankan pipeline impor sungguhan di PDF TA."""

import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\Documents\project ta\Nalar.ai-be")

from app.services.doc_import import pdf_to_markdown, _rapikan

SUMBER = r"C:\Users\Administrator\Documents\project ta\Laporan_Tugas_Akhir_Nalar_AI.pdf"
IMAGES = r"C:\Users\ADMINI~1\AppData\Local\Temp\opencode\diag_import\images"

contents = open(SUMBER, "rb").read()
print(f"PDF: {len(contents)//1024} KB")

t0 = time.time()
md = pdf_to_markdown(contents, IMAGES, "http://localhost:8087/uploads/d/images", "d")
print(f"ekstraksi: {time.time()-t0:.1f}s, markdown {len(md)//1024} KB, baris {md.count(chr(10))}")

on = md.splitlines()
print("=== 60 baris pertama ===")
for ln in on[:60]:
    print(repr(ln) if len(ln) > 120 else ln)

print("\n=== heading (## ) ===")
for i, ln in enumerate(on):
    if ln.startswith("## "):
        print(i, ln[:90])