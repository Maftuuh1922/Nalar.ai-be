"""Pencarian referensi akademik nyata untuk klaim yang belum tersitasi.

Metadata berasal dari Crossref/OpenAlex, bukan dari tebakan model. DOI hanya
dikembalikan setelah diverifikasi melalui doi.org.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx


def _authors(items: list[dict[str, Any]] | None) -> list[str]:
    result: list[str] = []
    for item in items or []:
        name = item.get("name") or " ".join(filter(None, [item.get("given"), item.get("family")]))
        if name:
            result.append(str(name).strip())
    return result[:12]


def _clean_doi(value: str | None) -> str | None:
    if not value:
        return None
    doi = re.sub(r"^https?://doi.org/", "", str(value).strip(), flags=re.I).rstrip(".,;)")
    return doi if doi.lower().startswith("10.") else None


async def _verify_doi(client: httpx.AsyncClient, doi: str) -> bool:
    try:
        response = await client.get(f"https://doi.org/{doi}", follow_redirects=True, timeout=12)
        return response.status_code < 400
    except httpx.HTTPError:
        return False


async def search_academic_references(query: str, limit: int = 5) -> list[dict[str, Any]]:
    query = re.sub(r"\s+", " ", query or "").strip()
    if len(query) < 8:
        return []
    limit = max(1, min(int(limit), 10))
    headers = {"User-Agent": "NALARAI/1.0 (academic-reference-search)"}
    async with httpx.AsyncClient(headers=headers) as client:
        crossref_url = "https://api.crossref.org/works"
        openalex_url = "https://api.openalex.org/works"
        crossref_task = client.get(crossref_url, params={"query.bibliographic": query, "filter": "type:journal-article", "rows": limit * 2}, timeout=20)
        openalex_task = client.get(openalex_url, params={"search": query, "filter": "type:article,from_publication_date:2018-01-01", "per-page": limit * 2}, timeout=20)
        responses = await asyncio.gather(crossref_task, openalex_task, return_exceptions=True)
        candidates: dict[str, dict[str, Any]] = {}
        crossref = responses[0]
        if isinstance(crossref, httpx.Response) and crossref.is_success:
            for item in crossref.json().get("message", {}).get("items", []):
                doi = _clean_doi(item.get("DOI"))
                title = (item.get("title") or [""])[0].strip()
                if not doi or not title:
                    continue
                date = item.get("published-print") or item.get("published-online") or item.get("issued") or {}
                year = (date.get("date-parts") or [[None]])[0][0]
                candidates[doi] = {"title": title, "authors": _authors(item.get("author")), "year": year, "doi": doi, "venue": (item.get("container-title") or [""])[0], "pages": item.get("page"), "source": "Crossref"}
        openalex = responses[1]
        if isinstance(openalex, httpx.Response) and openalex.is_success:
            for item in openalex.json().get("results", []):
                doi = _clean_doi((item.get("doi") or "").replace("https://doi.org/", ""))
                title = (item.get("title") or "").strip()
                if not doi or not title or doi in candidates:
                    continue
                candidates[doi] = {"title": title, "authors": _authors(item.get("authorships")), "year": item.get("publication_year"), "doi": doi, "venue": ((item.get("primary_location") or {}).get("source") or {}).get("display_name", ""), "pages": None, "source": "OpenAlex"}
        values = list(candidates.values())[: limit * 2]
        verified = await asyncio.gather(*(_verify_doi(client, item["doi"]) for item in values), return_exceptions=True)
        return [item for item, ok in zip(values, verified) if ok is True][:limit]
