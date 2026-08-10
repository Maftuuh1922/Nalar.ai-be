"""Hitung struktur LaTeX pada satu berkas .tex — pembanding cepat antar tahap.

Dipakai untuk membuktikan pada tahap mana isi laporan hilang: kalau angkanya
sudah timpang di berkas .tex, kerusakan terjadi saat impor; kalau .tex utuh tapi
PDF-nya timpang, kerusakan terjadi saat ekspor.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

POLA = [
    r"\\chapter\*?\{",
    r"\\section\{",
    r"\\section\*\{",
    r"\\subsection\{",
    r"\\subsection\*\{",
    r"\\subsubsection\*?\{",
    r"\\includegraphics",
    r"\\begin\{tabular\}",
    r"\\begin\{longtable\}",
    r"\\begin\{table\}",
    r"\\caption\{",
    r"\\newpage",
    r"\\clearpage",
    r"\\thispagestyle",
    r"\\begin\{center\}",
    r"\\geometry",
    r"\\multicolumn",
    r"\\multirow",
    r"\\dotfill",
    r"\\textbf\{",
    r"\\begin\{itemize\}",
    r"\\begin\{enumerate\}",
]


def main() -> int:
    teks = Path(sys.argv[1]).read_text(encoding="utf-8")
    for pola in POLA:
        print(f"{len(re.findall(pola, teks)):>5}  {pola}")
    i = teks.find(r"\begin{document}")
    print(f"\npanjang total {len(teks)} aksara, preamble {i} aksara")
    print("--- preamble ---")
    print(teks[: min(i, 1400)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
