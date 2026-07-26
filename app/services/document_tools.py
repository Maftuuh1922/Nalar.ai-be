"""Modul tool untuk membaca dan mencari dokumen secara agentic."""

import asyncio
import json
import logging
import re
from typing import Any
import uuid

# Paket `duckduckgo_search` sudah tidak dirawat dan backend-nya sering
# mengembalikan nol hasil. `ddgs` adalah kelanjutannya; paket lama dipakai
# hanya sebagai cadangan bila `ddgs` belum terpasang.
try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - hanya untuk lingkungan lama
    from duckduckgo_search import DDGS

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from llama_index.core import SimpleDirectoryReader

from app.models.document import Document

logger = logging.getLogger(__name__)

async def list_documents(db: AsyncSession, user_id: uuid.UUID, **kwargs) -> str:
    """Mengembalikan list dokumen milik user."""
    try:
        user_docs = await db.scalars(
            select(Document).where(Document.user_id == user_id)
        )
        docs = user_docs.all()
        if not docs:
            return "Kamu belum mengunggah dokumen apapun."
            
        result = []
        for d in docs:
            # Sembunyikan status "failed" yang berkaitan dengan vector DB, karena agentic tool tetap bisa baca file mentahnya
            status_info = d.status if d.status == "indexed" else "tersedia untuk dibaca"
            result.append(f"- ID: {d.id} | Nama File: {d.filename} | Status: {status_info}")
        return "\n".join(result)
    except Exception as e:
        logger.error(f"Error in list_documents: {e}")
        return f"ERROR saat mengambil daftar dokumen: {e}"

async def read_document(db: AsyncSession, user_id: uuid.UUID, document_id_or_filename: str, section: str | None = None, max_chars: int = 8000, **kwargs) -> str:
    """Membaca isi teks dari sebuah dokumen."""
    try:
        # Cari dokumen (bisa berdasarkan ID atau nama)
        stmt = select(Document).where(Document.user_id == user_id)
        try:
            doc_uuid = uuid.UUID(document_id_or_filename)
            stmt = stmt.where(Document.id == doc_uuid)
        except ValueError:
            stmt = stmt.where(Document.filename.ilike(f"%{document_id_or_filename}%"))
            
        doc = await db.scalar(stmt)
        if not doc:
            return json.dumps({"error": f"Dokumen dengan pencarian '{document_id_or_filename}' tidak ditemukan."})

        # Coba parse pakai LlamaIndex SimpleDirectoryReader
        try:
            reader = SimpleDirectoryReader(input_files=[doc.file_path])
            parsed_docs = reader.load_data()
            extracted_parts = []
            for pd in parsed_docs:
                if pd.text:
                    page_label = pd.metadata.get("page_label") if pd.metadata else None
                    if page_label:
                        extracted_parts.append(f"[Halaman {page_label}]\n{pd.text}")
                    else:
                        extracted_parts.append(pd.text)
            extracted_text = "\n\n".join(extracted_parts)
        except Exception as parse_err:
            return json.dumps({"error": f"ERROR: Dokumen ditemukan tapi gagal diparsing (format tidak didukung / file korup): {parse_err}"})

        if not extracted_text.strip():
            return json.dumps({"error": "ERROR: Dokumen berhasil diparsing, tetapi tidak ada teks yang dapat diekstrak (dokumen mungkin kosong atau berupa gambar tanpa OCR)."})

        # Tentukan posisi section yang diminta
        # Untuk simplifikasi, jika tidak ada pagination/section, potong sesuai max_chars
        total_length = len(extracted_text)
        if total_length > max_chars:
            excerpt = extracted_text[:max_chars]
            return json.dumps({
                "message": f"Teks dokumen terlalu panjang ({total_length} karakter). Menampilkan {max_chars} karakter pertama. Gunakan search_in_document untuk mencari informasi yang lebih spesifik.",
                "content": excerpt
            })
            
        return json.dumps({"content": extracted_text})
        
    except Exception as e:
        logger.error(f"Error in read_document: {e}")
        return json.dumps({"error": f"Terjadi kesalahan sistem saat membaca dokumen: {e}"})


async def search_in_document(db: AsyncSession, user_id: uuid.UUID, document_id_or_filename: str, query: str, max_matches: int = 5, **kwargs) -> str:
    """Mencari potongan teks (heuristic/regex dasar) di dalam suatu dokumen panjang."""
    try:
        stmt = select(Document).where(Document.user_id == user_id)
        try:
            doc_uuid = uuid.UUID(document_id_or_filename)
            stmt = stmt.where(Document.id == doc_uuid)
        except ValueError:
            stmt = stmt.where(Document.filename.ilike(f"%{document_id_or_filename}%"))
            
        doc = await db.scalar(stmt)
        if not doc:
            return json.dumps({"error": f"Dokumen '{document_id_or_filename}' tidak ditemukan."})

        try:
            reader = SimpleDirectoryReader(input_files=[doc.file_path])
            parsed_docs = reader.load_data()
            extracted_parts = []
            for pd in parsed_docs:
                if pd.text:
                    page_label = pd.metadata.get("page_label") if pd.metadata else None
                    if page_label:
                        extracted_parts.append(f"[Halaman {page_label}]\n{pd.text}")
                    else:
                        extracted_parts.append(pd.text)
            extracted_text = "\n\n".join(extracted_parts)
        except Exception as parse_err:
            return json.dumps({"error": f"Gagal membaca dokumen untuk pencarian: {parse_err}"})

        # Pencarian substring sederhana (heuristic grep-like)
        lines = extracted_text.split("\n")
        query_lower = query.lower()
        matches = []
        
        for i, line in enumerate(lines):
            if query_lower in line.lower():
                # Ambil konteks: 2 baris sebelum dan sesudah
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                context = "\n".join(lines[start:end])
                
                if context not in [m["context"] for m in matches]: # Hindari duplikat overlapping
                    matches.append({"line": i+1, "context": context.strip()})
                    
                if len(matches) >= max_matches:
                    break
                    
        if not matches:
            return json.dumps({"message": f"Kata kunci '{query}' tidak ditemukan di dalam dokumen ini."})
            
        return json.dumps({"matches": matches})

    except Exception as e:
        logger.error(f"Error in search_in_document: {e}")
        return json.dumps({"error": f"Kesalahan pencarian: {e}"})

# Urutan backend pencarian yang dicoba. Satu backend bisa kosong atau kena
# pembatasan sesaat, jadi jangan menyerah setelah percobaan pertama.
_SEARCH_BACKENDS = ("auto", "brave", "duckduckgo", "bing")


def _run_search(query: str, max_results: int, backend: str) -> list[dict[str, Any]]:
    """Satu percobaan pencarian (blocking) memakai backend tertentu."""
    try:
        return DDGS().text(query, max_results=max_results, backend=backend) or []
    except TypeError:
        # Versi lama tidak menerima argumen `backend`.
        return DDGS().text(query, max_results=max_results) or []


async def search_web(query: str, max_results: int = 5, **kwargs) -> str:
    """Mencari informasi di internet.

    Beberapa backend dicoba bergantian karena satu penyedia sering
    mengembalikan nol hasil tanpa alasan jelas. Pesan balasan sengaja
    menyarankan langkah lanjutan supaya agen mencoba kata kunci lain
    alih-alih menyerah dan meminta maaf ke user.
    """
    query = (query or "").strip()
    if not query:
        return json.dumps({"error": "Kata kunci pencarian kosong."})

    try:
        max_results = max(1, min(int(max_results or 5), 20))
    except (TypeError, ValueError):
        max_results = 5

    errors: list[str] = []
    for backend in _SEARCH_BACKENDS:
        try:
            # DDGS bersifat blocking; jalankan di thread agar event loop bebas.
            results = await asyncio.to_thread(_run_search, query, max_results, backend)
        except Exception as exc:
            errors.append(f"{backend}: {exc}")
            logger.warning(f"search_web backend '{backend}' gagal: {exc}")
            continue

        formatted = [
            {
                "title": r.get("title", ""),
                "body": r.get("body") or r.get("description", ""),
                "url": r.get("href") or r.get("url", ""),
            }
            for r in results
            if r.get("href") or r.get("url")
        ]
        if formatted:
            return json.dumps({"results": formatted, "backend": backend})
        errors.append(f"{backend}: kosong")

    logger.info(f"search_web tidak menemukan hasil untuk '{query}' ({'; '.join(errors)})")
    return json.dumps({
        "results": [],
        "message": (
            f"Belum ada hasil untuk '{query}'. Coba lagi dengan kata kunci lain "
            "(lebih umum, bahasa Inggris, atau tambahkan 'filetype:pdf'). "
            "Jangan menyerah setelah satu percobaan."
        ),
    })

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal", "169.254.169.254"}
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript", "form", "svg", "iframe")


async def fetch_webpage(url: str, max_chars: int = 8000, **kwargs) -> str:
    """Membuka sebuah URL dan mengembalikan isi teks halamannya.

    Dipakai agen ketika cuplikan dari ``search_web`` belum cukup dan ia perlu
    melihat detail isi halaman sebelum mengutipnya di laporan.
    """
    import httpx
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse

    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return json.dumps({"error": "URL harus diawali http:// atau https://"})
    # Jangan biarkan model mengarahkan permintaan ke jaringan internal server.
    if (parsed.hostname or "").lower() in _BLOCKED_HOSTS:
        return json.dumps({"error": "Alamat internal tidak boleh diakses."})

    try:
        max_chars = max(500, min(int(max_chars or 8000), 20000))
    except (TypeError, ValueError):
        max_chars = 8000

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NalarAI/1.0; +https://nalar.ai)"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return json.dumps({"error": f"Jenis konten tidak didukung: {content_type or 'tidak diketahui'}"})

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(list(_STRIP_TAGS)):
            tag.decompose()

        title = soup.title.get_text(strip=True) if soup.title else url
        main = soup.find("article") or soup.find("main") or soup.body or soup
        text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))

        truncated = len(text) > max_chars
        return json.dumps({
            "url": str(response.url),
            "title": title,
            "text": text[:max_chars],
            "truncated": truncated,
            "chars": len(text),
        })
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"Halaman menolak permintaan (HTTP {e.response.status_code})."})
    except Exception as e:
        logger.error(f"Error in fetch_webpage({url}): {e}")
        return json.dumps({"error": f"Gagal membuka halaman: {e}"})


# Skema OpenAI untuk tool calling
DOCUMENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": "Mengambil daftar dokumen (ID, nama, status) yang telah diunggah oleh user.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Membaca isi teks dari dokumen. Gunakan ini jika dokumen pendek atau Anda butuh keseluruhan konteks awal. Jika dokumen terlalu panjang, teks akan dipotong.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id_or_filename": {
                        "type": "string",
                        "description": "UUID atau nama file dokumen."
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Jumlah maksimal karakter yang dikembalikan (default 8000)."
                    }
                },
                "required": ["document_id_or_filename"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_document",
            "description": "Mencari potongan teks spesifik di dalam dokumen berdasarkan kata kunci query. Sangat berguna untuk mengekstrak informasi detail dari dokumen yang panjang tanpa membaca seluruh dokumen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id_or_filename": {
                        "type": "string",
                        "description": "UUID atau nama file dokumen."
                    },
                    "query": {
                        "type": "string",
                        "description": "Kata kunci untuk dicari."
                    }
                },
                "required": ["document_id_or_filename", "query"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Mencari informasi, jurnal, atau berita di internet secara real-time. Gunakan ini jika informasi tidak ada di dokumen yang diunggah pengguna.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Kata kunci pencarian internet."
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Jumlah maksimal hasil yang dikembalikan (default 5)."
                    }
                },
                "required": ["query"],
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_webpage",
            "description": (
                "Membuka satu URL hasil pencarian dan membaca ISI LENGKAP halamannya. "
                "Gunakan setelah search_web ketika cuplikan hasil pencarian belum cukup "
                "untuk menulis laporan atau saat kamu perlu mengutip detail, angka, dan "
                "kutipan yang akurat dari sumber tersebut."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL lengkap halaman yang ingin dibaca (harus http/https)."
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Batas jumlah karakter teks yang dikembalikan (default 8000)."
                    }
                },
                "required": ["url"],
                "additionalProperties": False
            }
        }
    }
]
