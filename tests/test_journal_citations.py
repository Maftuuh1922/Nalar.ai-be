"""Unit test logika murni fitur Referensi Jurnal & Sitasi.

Tidak butuh server hidup maupun database: hanya fungsi-fungsi tanpa akses
session DB (citation_formatter & agentic_writer murni). Kontrak API penuh
diuji terpisah lewat smoke test terhadap backend 8087.
"""

import pytest

from app.services.agentic_writer import replace_citation_markers
from app.services.citation_formatter import (
    CITATION_FORMATS,
    CitationError,
    citation_meta_from_reference,
    format_apa,
    format_harvard,
    format_ieee,
    format_mla,
    generate_citation,
)

# Contoh metadata jurnal nyata (mirip daftar_jurnal_referensi.md).
META = {
    "title": "Evaluating Large Language Models in Retrieval-Augmented Tutoring Systems",
    "authors": ["Bhat, O.", "Jeelani, Z.", "Rabani, S.", "Saif, S.", "Lone, N."],
    "year": 2026,
    "journal_name": "SN Computer Science",
    "volume": "7",
    "issue": "3",
    "pages": "273",
    "doi": "10.1007/s42979-026-04789-w",
    "publisher": "Springer",
}


class TestCitationFormats:
    def test_all_formats_known(self):
        for fmt in CITATION_FORMATS:
            assert generate_citation(META, fmt).strip()

    def test_ieee_joins_authors_with_and(self):
        citation = format_ieee(META)
        assert "Bhat, O., Jeelani, Z., Rabani, S., Saif, S., and Lone, N." in citation
        assert '," SN Computer Science, vol. 7, no. 3, pp. 273, 2026.' in citation

    def test_ieee_two_authors(self):
        meta = dict(META, authors=["Bhat, O.", "Jeelani, Z."])
        assert "Bhat, O. and Jeelani, Z.," in format_ieee(meta)

    def test_apa_contains_doi_and_volume(self):
        citation = format_apa(META)
        assert "https://doi.org/10.1007/s42979-026-04789-w" in citation
        assert "(2026)." in citation
        assert "vol. 7, no. 3" in citation

    def test_mla_title_case(self):
        citation = format_mla(META)
        assert "Evaluating Large Language Models in Retrieval-Augmented Tutoring Systems" in citation

    def test_harvard_year_italic_title(self):
        citation = format_harvard(META)
        assert "(2026)" in citation
        assert "*Evaluating" in citation

    def test_missing_title_raises(self):
        with pytest.raises(CitationError):
            generate_citation({"year": 2026, "authors": []}, "ieee")

    def test_unknown_format_raises(self):
        with pytest.raises(CitationError):
            generate_citation(META, "nonsense")

    def test_no_authors_ok(self):
        meta = dict(META, authors=[])
        citation = generate_citation(meta, "ieee")
        assert citation.startswith('"Evaluating')


class TestCitationMetaFromReference:
    def test_from_object(self):
        class Ref:
            title = "T"
            authors = ["A, B."]
            year = 2026
            journal_name = "J"
            volume = "1"
            issue = None
            pages = None
            doi = None
            publisher = None

        meta = citation_meta_from_reference(Ref())
        assert meta["title"] == "T"
        assert meta["authors"] == ["A, B."]

    def test_from_dict(self):
        meta = citation_meta_from_reference({"title": "X", "authors": ["C, D."], "year": 2020})
        assert meta["title"] == "X"
        assert meta["year"] == 2020


class TestReplaceCitationMarkers:
    def test_markers_replaced_with_numbers(self):
        class Ref:
            def __init__(self, title, year):
                self.title = title
                self.authors = ["A, B."]
                self.year = year
                self.journal_name = "J"
                self.volume = None
                self.issue = None
                self.pages = None
                self.doi = None
                self.publisher = None

        refs = [Ref("Paper Satu", 2020), Ref("Paper Dua", 2021)]
        text = "Menurut [1], RAG membantu [2]. Kombinasi [1][2] kuat."
        replaced, used = replace_citation_markers(text, refs, "ieee")
        assert "[1]" in replaced
        assert "[2]" in replaced
        assert len(used) == 4  # [1] 2x, [2] 2x ([1][2] menempel = 2 marker)
        assert "Paper Satu" in used[0]
        assert "Paper Dua" in used[1]

    def test_out_of_range_marker_kept(self):
        text = "Lihat [9] yang tidak ada."
        replaced, used = replace_citation_markers(text, [], "ieee")
        assert "[9]" in replaced
        assert used == []
