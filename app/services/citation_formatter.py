"""Generator sitasi akademik — murni Python, tanpa LLM.

Mengubah metadata referensi jurnal menjadi sitasi dalam 7 format:
IEEE, APA 7th, MLA, Chicago Notes-Bibliography, Harvard, Vancouver, dan
SNI/gaya Indonesia. Deterministik dan cepat, sehingga frontend bisa
mendapatkan hasil instan.

Input metadata mengikuti kolom JournalReference:
    title, authors (list[str]), year, journal_name, volume, issue,
    pages, doi, publisher
"""

from __future__ import annotations

CITATION_FORMATS: tuple[str, ...] = (
    "ieee",
    "apa",
    "mla",
    "chicago",
    "harvard",
    "vancouver",
    "sni",
)

FORMAT_LABELS: dict[str, str] = {
    "ieee": "IEEE",
    "apa": "APA 7th",
    "mla": "MLA",
    "chicago": "Chicago (Notes-Bibliography)",
    "harvard": "Harvard",
    "vancouver": "Vancouver",
    "sni": "SNI / Gaya Indonesia",
}


class CitationError(ValueError):
    """Metadata tidak cukup untuk membuat sitasi."""


def _authors_list(authors: list[str] | None) -> list[str]:
    """Normalisasi daftar penulis menjadi list string bersih."""
    if not authors:
        return []
    cleaned: list[str] = []
    for author in authors:
        if not author:
            continue
        text = str(author).strip()
        # Format "LastName, FirstName" atau "FirstName LastName" — keduanya dipakai apa adanya.
        if text:
            cleaned.append(text)
    return cleaned


def _require(meta: dict, *fields: str) -> None:
    """Raises CitationError bila field wajib kosong."""
    for field in fields:
        value = meta.get(field)
        if value is None or str(value).strip() == "":
            raise CitationError(f"Metadata tidak lengkap: {field} belum diisi.")


def _initials(full_name: str) -> str:
    """Ubah 'John A. Smith' → 'J. A. Smith' (atau biarkan bila sudah berinisial)."""
    parts = str(full_name).strip().split()
    if len(parts) <= 1:
        return full_name.strip()
    out: list[str] = []
    for part in parts:
        # Bagian yang sudah berupa inisial (mis. "A." atau "A") dipertahankan.
        if part.endswith(".") and len(part) <= 3:
            out.append(part)
        elif len(part) == 1:
            out.append(f"{part}.")
        else:
            out.append(part)
    return " ".join(out)


def _first_author_last_first(authors: list[str]) -> str:
    """Penulis pertama dalam format 'NamaBelakang, Inisial' (APA/IEEE)."""
    if not authors:
        return ""
    first = authors[0].strip()
    if "," in first:
        last, _, given = first.partition(",")
        return f"{last.strip()}, {_initials(given.strip())}"
    parts = first.rsplit(" ", 1)
    if len(parts) == 2:
        return f"{parts[1]}, {_initials(parts[0])}"
    return first


def _last_first_all(authors: list[str], max_count: int = 6) -> str:
    """Semua penulis format 'Last, I.' dipisah koma; 'et al.' bila lebih dari max_count."""
    if not authors:
        return ""
    formatted = [_first_author_last_first([a]) if "," not in a else a for a in authors]
    if len(formatted) > max_count:
        return ", ".join(formatted[: max_count - 1]) + ", et al."
    if len(formatted) == 2:
        return f"{formatted[0]} and {formatted[1]}"
    return ", ".join(formatted)


def _title_case(text: str) -> str:
    """Judul gaya MLA (kata utama kapital) — sederhana: kapital tiap kata kecuali kata sambung pendek."""
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "via", "with"}
    words = text.split()
    if not words:
        return text
    out = []
    for i, word in enumerate(words):
        lower = word.lower()
        if i == 0 or i == len(words) - 1 or lower not in small:
            out.append(word[0].upper() + word[1:] if word else word)
        else:
            out.append(lower)
    return " ".join(out)


def _vol_issue(meta: dict) -> str:
    """'vol. 5, no. 2' — hanya bagian yang ada."""
    parts = []
    if meta.get("volume"):
        parts.append(f"vol. {meta['volume']}")
    if meta.get("issue"):
        parts.append(f"no. {meta['issue']}")
    return ", ".join(parts)


def _pages(meta: dict) -> str:
    return f"pp. {meta['pages']}" if meta.get("pages") else ""


def _doi_url(meta: dict) -> str:
    doi = (meta.get("doi") or "").strip()
    if not doi:
        return ""
    if doi.startswith("http"):
        return doi
    return f"https://doi.org/{doi}"


def format_ieee(meta: dict) -> str:
    """[1] A. Author and B. Author, "Title," Journal, vol. x, no. y, pp. z, 2026."""
    _require(meta, "title", "year")
    authors = _authors_list(meta.get("authors"))
    year = meta["year"]
    title = str(meta["title"]).strip().rstrip(".")
    journal = str(meta.get("journal_name") or "").strip().rstrip(".")
    body = f'"{title},"'
    if journal:
        body += f" {journal}"
    vi = _vol_issue(meta)
    if vi:
        body += f", {vi}"
    pg = _pages(meta)
    if pg:
        body += f", {pg}"
    body += f", {year}."
    if not authors:
        return body
    initials = [_initials(a) for a in authors[:6]]
    if len(initials) == 1:
        names = initials[0]
    elif len(initials) == 2:
        names = f"{initials[0]} and {initials[1]}"
    else:
        names = ", ".join(initials[:-1]) + f", and {initials[-1]}"
    return f"{names}, {body}"


def format_apa(meta: dict) -> str:
    """Author, A. (2026). Title. Journal, 5(2), 10-20. https://doi.org/xxx"""
    _require(meta, "title", "year")
    authors = _authors_list(meta.get("authors"))
    year = meta["year"]
    title = str(meta["title"]).strip().rstrip(".")
    journal = str(meta.get("journal_name") or "").strip()
    if authors:
        author_part = _last_first_all(authors, max_count=20)
    else:
        author_part = ""
    ref = f"{author_part} ({year}). {title}." if author_part else f"({year}). {title}."
    if journal:
        ref += f" *{journal}*"
        vi = _vol_issue(meta)
        if vi:
            ref += f", {vi}"
        if meta.get("pages"):
            ref += f", {meta['pages']}"
        ref += "."
    doi = _doi_url(meta)
    if doi:
        ref += f" {doi}"
    return ref


def format_mla(meta: dict) -> str:
    """Author, A. "Title." Journal, vol. 5, no. 2, 2026, pp. 10-20."""
    _require(meta, "title", "year")
    authors = _authors_list(meta.get("authors"))
    title = _title_case(str(meta["title"]).strip().rstrip("."))
    journal = str(meta.get("journal_name") or "").strip()
    ref = f'"{title}."'
    if journal:
        ref += f" *{journal}*,"
    parts = []
    if meta.get("volume"):
        parts.append(f"vol. {meta['volume']}")
    if meta.get("issue"):
        parts.append(f"no. {meta['issue']}")
    if parts:
        ref += " " + ", ".join(parts) + ","
    ref += f" {meta['year']}"
    if meta.get("pages"):
        ref += f", pp. {meta['pages']}"
    ref += "."
    if not authors:
        return ref
    return f"{_first_author_last_first(authors)} {ref}"


def format_chicago(meta: dict) -> str:
    """Author, A. "Title." Journal 5, no. 2 (2026): 10-20."""
    _require(meta, "title", "year")
    authors = _authors_list(meta.get("authors"))
    title = _title_case(str(meta["title"]).strip().rstrip("."))
    journal = str(meta.get("journal_name") or "").strip()
    ref = f'"{title}."'
    if journal:
        vol = f" {meta['volume']}" if meta.get("volume") else ""
        issue = f", no. {meta['issue']}" if meta.get("issue") else ""
        ref += f" {journal}{vol}{issue} ({meta['year']})"
    else:
        ref += f" ({meta['year']})"
    if meta.get("pages"):
        ref += f": {meta['pages']}"
    ref += "."
    if not authors:
        return ref
    return f"{_first_author_last_first(authors)} {ref}"


def format_harvard(meta: dict) -> str:
    """Author, A. (2026) Title. Journal, vol 5(2), pp. 10-20."""
    _require(meta, "title", "year")
    authors = _authors_list(meta.get("authors"))
    year = meta["year"]
    title = str(meta["title"]).strip().rstrip(".")
    journal = str(meta.get("journal_name") or "").strip()
    ref = f"({year}) *{title}*."
    if journal:
        ref += f" {journal}"
        vol = f" vol {meta['volume']}" if meta.get("volume") else ""
        issue = f"({meta['issue']})" if meta.get("issue") else ""
        ref += f",{vol}{issue}"
        if meta.get("pages"):
            ref += f", pp. {meta['pages']}"
    ref += "."
    if not authors:
        return ref
    return f"{_first_author_last_first(authors)} {ref}"


def format_vancouver(meta: dict) -> str:
    """1. Author A. Title. Journal. Year;Vol(Issue):Pages."""
    _require(meta, "title", "year")
    authors = _authors_list(meta.get("authors"))
    title = str(meta["title"]).strip().rstrip(".")
    journal = str(meta.get("journal_name") or "").strip()
    ref = f"{title}. {journal}." if journal else f"{title}."
    vi = ""
    vol = meta.get("volume")
    issue = meta.get("issue")
    if vol or issue:
        vi = f"{vol or ''}({issue or ''})" if issue else str(vol)
    tail = f" {meta['year']}"
    if vi:
        tail += f";{vi}"
    if meta.get("pages"):
        tail += f":{meta['pages']}"
    ref += tail
    if not authors:
        return ref
    names = " ".join(_initials(a) for a in authors[:6])
    return f"{names}. {ref}"


def format_sni(meta: dict) -> str:
    """Penulis, Tahun. Judul. Nama Jurnal, vol(issue): halaman. DOI.

    Mengikuti gaya umum sitasi jurnal nasional Indonesia (SNI/kaidah
    perguruan tinggi): NamaBelakang, Inisial; Tahun; Judul miring;
    Jurnal miring; volume(issue): halaman; tautan DOI.
    """
    _require(meta, "title", "year")
    authors = _authors_list(meta.get("authors"))
    year = meta["year"]
    title = str(meta["title"]).strip().rstrip(".")
    journal = str(meta.get("journal_name") or "").strip()
    if authors:
        ref = f"{_last_first_all(authors, max_count=6)} ({year}). *{title}*."
    else:
        ref = f"({year}). *{title}*."
    if journal:
        ref += f" *{journal}*"
        vol = f"{meta['volume']}" if meta.get("volume") else ""
        issue = f"({meta['issue']})" if meta.get("issue") else ""
        if vol or issue:
            ref += f", {vol}{issue}"
        if meta.get("pages"):
            ref += f": {meta['pages']}"
        ref += "."
    doi = _doi_url(meta)
    if doi:
        ref += f" {doi}"
    return ref


_FORMATTERS: dict[str, callable] = {
    "ieee": format_ieee,
    "apa": format_apa,
    "mla": format_mla,
    "chicago": format_chicago,
    "harvard": format_harvard,
    "vancouver": format_vancouver,
    "sni": format_sni,
}


def generate_citation(meta: dict, format_name: str = "ieee") -> str:
    """Generate satu sitasi dari metadata.

    Args:
        meta: dict berisi title, authors, year, journal_name, volume,
              issue, pages, doi, publisher.
        format_name: salah satu dari CITATION_FORMATS.

    Raises:
        CitationError: format tidak dikenal atau metadata tidak lengkap.
    """
    key = (format_name or "ieee").lower().strip()
    if key not in _FORMATTERS:
        raise CitationError(f"Format sitasi tidak dikenal: {format_name}. "
                            f"Pilihan: {', '.join(CITATION_FORMATS)}")
    return _FORMATTERS[key](meta)


def citation_meta_from_reference(reference) -> dict:
    """Bangun dict metadata dari objek model JournalReference (atau dict serupa)."""
    authors = reference.authors if hasattr(reference, "authors") else (reference.get("authors") if isinstance(reference, dict) else None)
    return {
        "title": reference.title if hasattr(reference, "title") else reference.get("title", ""),
        "authors": authors,
        "year": reference.year if hasattr(reference, "year") else reference.get("year"),
        "journal_name": reference.journal_name if hasattr(reference, "journal_name") else reference.get("journal_name", ""),
        "volume": reference.volume if hasattr(reference, "volume") else reference.get("volume"),
        "issue": reference.issue if hasattr(reference, "issue") else reference.get("issue"),
        "pages": reference.pages if hasattr(reference, "pages") else reference.get("pages"),
        "doi": reference.doi if hasattr(reference, "doi") else reference.get("doi"),
        "publisher": reference.publisher if hasattr(reference, "publisher") else reference.get("publisher"),
    }
