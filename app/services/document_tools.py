"""Modul tool untuk membaca dan mencari dokumen secara agentic."""

import json
import logging
from typing import Any
import uuid
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

async def search_web(query: str, max_results: int = 5, **kwargs) -> str:
    """Mencari informasi di internet menggunakan DuckDuckGo."""
    try:
        results = DDGS().text(query, max_results=max_results)
        if not results:
            return json.dumps({"message": f"Tidak ada hasil pencarian untuk '{query}'"})
            
        formatted_results = []
        for r in results:
            formatted_results.append({
                "title": r.get("title", ""),
                "body": r.get("body", ""),
                "url": r.get("href", "")
            })
            
        return json.dumps({"results": formatted_results})
    except Exception as e:
        logger.error(f"Error in search_web: {e}")
        return json.dumps({"error": f"Kesalahan pencarian web: {e}"})

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
    }
]
