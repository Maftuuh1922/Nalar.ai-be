"""Lacak rugi impor per tahap: DOCX -> Markdown -> LaTeX.

Ketiga angka dicetak berdampingan supaya jelas tahap mana yang membuang apa.
Tanpa ini mudah menyalahkan pengekspor untuk kerugian yang sudah terjadi di
pengekstrak.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from app.services.doc_import import docx_to_markdown
from app.services.latex_export import markdown_to_latex

PENANDA_MD = {
    "heading '# '": r"(?m)^#{1,6} ",
    "tebal '**'": r"\*\*[^*\n]{1,120}\*\*",
    "miring '*'": r"(?<!\*)\*[^*\n]{1,120}\*(?!\*)",
    "gambar '![]('": r"!\[[^\]]*\]\(",
    "baris tabel '|'": r"(?m)^\|",
    # `<newpage>` adalah penanda internal yang dipahami markdown_to_latex.
    "jeda halaman": r"(?m)^<newpage>$|<!-- *pagebreak *-->|\f",
}

PENANDA_TEX = {
    "sectioning": r"\\(?:chapter|section|subsection|subsubsection|paragraph)\*?\{",
    "tebal": r"\\textbf\{",
    "miring": r"\\(?:textit|emph)\{",
    "gambar": r"\\includegraphics",
    "tabel": r"\\begin\{tabular\}",
    "caption": r"\\caption\{",
    "jeda halaman": r"\\newpage|\\clearpage",
}


def main() -> int:
    docx = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as tmp:
        md = docx_to_markdown(
            docx.read_bytes(), str(Path(tmp) / "img"), "http://x/img", "diag"
        )
    tex = markdown_to_latex(md, preserve_source=True)

    print(f"Markdown hasil ekstraksi DOCX: {len(md)} aksara")
    for nama, pola in PENANDA_MD.items():
        print(f"  {len(re.findall(pola, md)):>5}  {nama}")
    print(f"\nLaTeX hasil konversi: {len(tex)} aksara")
    for nama, pola in PENANDA_TEX.items():
        print(f"  {len(re.findall(pola, tex)):>5}  {nama}")
    Path("diag_impor.md").write_text(md, encoding="utf-8")
    Path("diag_impor.tex").write_text(tex, encoding="utf-8")
    print("\ndiag_impor.md dan diag_impor.tex ditulis untuk pemeriksaan manual")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
