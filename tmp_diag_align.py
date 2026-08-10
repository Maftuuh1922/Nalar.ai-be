# -*- coding: utf-8 -*-
"""Periksa perataan baris di PDF asli halaman muka (1-9)."""
import sys
import fitz

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
doc = fitz.open(r"uploads/bf181917-d402-4be8-85f3-e9de81d201e9/source/original.pdf")

for pno in range(0, 9):
    page = doc[pno]
    W = page.rect.width
    mid = W / 2
    print(f"\n===== HALAMAN {pno+1} (lebar {round(W)}) =====")
    lines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            bbox = line["bbox"]
            x0, y0, x1, y1 = bbox
            teks = "".join(s.get("text", "") for s in line.get("spans", []))
            if not teks.strip():
                continue
            cx = (x0 + x1) / 2
            # perataan kasar: center bila tengah baris dekat tengah halaman
            # dan tepi kiri menjauh dari margin kiri
            align = "CENTER" if abs(cx - mid) < 25 else "LEFT/JUST"
            lines.append((y0, align, teks.strip()[:80]))
    for y0, align, teks in lines:
        print(f"  y={y0:>6.1f} [{align:>9}] {teks}")
doc.close()
