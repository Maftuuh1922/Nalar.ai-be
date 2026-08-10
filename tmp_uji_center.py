# -*- coding: utf-8 -*-
"""Uji cepat: apakah \\subsection* di dalam \\begin{center} di-center di compile?"""
import io
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.services.latex_export import _PREAMBLE

sample = r"""
\begin{document}
\begin{center}
\subsection*{LEMBAR PENGESAHAN DOSEN PEMBIMBING}
\end{center}

\begin{center}
Di Bandung, \ldots 2026
\end{center}
\newpage
\begin{center}
\subsection*{LEMBAR PENGESAHAN DOSEN PENGUJI}
\end{center}
\end{document}
"""
tmpdir = tempfile.mkdtemp()
try:
    from app.services.latex_export import compile_latex_pdf
    pdf = compile_latex_pdf(_PREAMBLE + sample, tmpdir, "ujicenter", [])
except RuntimeError as e:
    print("COMPILE GAGAL:", str(e)[-800:])
    sys.exit(1)

import fitz
doc = fitz.open(pdf)
print("halaman:", len(doc))
W = doc[0].rect.width
for pno in range(len(doc)):
    print(f"\n=== halaman {pno+1} ===")
    for block in doc[pno].get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            b = line["bbox"]
            teks = "".join(s.get("text", "") for s in line.get("spans", []))
            if teks.strip():
                cx = (b[0] + b[2]) / 2
                print(f"  x0={b[0]:>6.1f} x1={b[2]:>6.1f} cx={cx:>6.1f} (mid={W/2:.1f}) {teks.strip()[:70]}")
doc.close()
