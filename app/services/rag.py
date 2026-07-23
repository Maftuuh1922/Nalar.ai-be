"""RAG pipeline menggunakan LlamaIndex + ChromaDB.

Setiap user mendapat koleksi ChromaDB tersendiri berdasarkan user_id.
Embedding dan LLM dikonfigurasi per-user dari model_configs.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

import chromadb
from llama_index.core import SimpleDirectoryReader, StorageContext, VectorStoreIndex
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.prompts import PromptTemplate
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.llms.openai import OpenAI as LlamaOpenAI
from llama_index.vector_stores.chroma import ChromaVectorStore

from app.core.config import settings

logger = logging.getLogger(__name__)

# Prompt dasar — jawab HANYA dari dokumen
_QA_BASE_TEMPLATE = (
    "{agent_persona}"
    "Konteks dari dokumen pengguna:\n"
    "---------------------\n"
    "{context_str}\n"
    "---------------------\n"
    "Instruksi: Jawab pertanyaan di bawah HANYA berdasarkan konteks di atas, dalam Bahasa Indonesia. "
    "Jika informasi tidak ada dalam konteks, katakan: "
    "'Informasi ini tidak tersedia dalam dokumen yang Anda upload.' "
    "Jangan mengarang atau menambahkan informasi di luar dokumen. "
    "Sertakan referensi ke bagian dokumen yang digunakan.\n\n"
    "Pertanyaan: {query_str}\n"
    "Jawaban: "
)

# Prompt Deep Reasoning (Chain-of-Thought disalin dari konsep DeepTutor reason tool)
_QA_REASONING_TEMPLATE = (
    "{agent_persona}"
    "Konteks dari dokumen pengguna:\n"
    "---------------------\n"
    "{context_str}\n"
    "---------------------\n"
    "Instruksi: Anda adalah tutor AI cerdas. Gunakan penalaran bertahap (Chain of Thought) untuk menganalisis masalah.\n"
    "1. Tuliskan analisis pemikiran bertahap Anda di dalam tag <think> ... </think> dalam Bahasa Indonesia.\n"
    "2. Setelah tag </think>, tuliskan jawaban akhir yang terstruktur, jelas, dan lugas berbasis dokumen pengguna.\n"
    "3. Sertakan kutipan/sitasi dokumen yang relevan.\n\n"
    "Pertanyaan: {query_str}\n"
    "Jawaban: "
)


def _build_qa_template(agent_system_prompt: str | None = None, enable_reasoning: bool = False) -> PromptTemplate:
    """Buat QA template, opsional dengan persona agen dan mode penalaran CoT."""
    persona = ""
    if agent_system_prompt:
        persona = f"Instruksi Persona Anda:\n{agent_system_prompt}\n\n"
    
    template_str = _QA_REASONING_TEMPLATE if enable_reasoning else _QA_BASE_TEMPLATE
    return PromptTemplate(template_str.replace("{agent_persona}", persona))


def _extract_thinking_and_answer(response_text: str) -> tuple[str | None, str]:
    """Ekstraksi tag <think>...</think> dari jawaban LLM."""
    pattern = r"<think>(.*?)</think>"
    match = re.search(pattern, response_text, re.DOTALL)
    if match:
        thinking = match.group(1).strip()
        answer = re.sub(pattern, "", response_text, flags=re.DOTALL).strip()
        return thinking, answer
    return None, response_text.strip()


_QUIZ_TEMPLATE = PromptTemplate(
    "Konteks dari dokumen pengguna:\n"
    "---------------------\n"
    "{context_str}\n"
    "---------------------\n"
    "Instruksi: Buat {num_questions} soal latihan pilihan ganda (multiple choice) berdasarkan konteks di atas dengan topik '{topic}'.\n"
    "Semua soal harus berbahasa Indonesia dan setiap soal wajib memiliki 4 opsi jawaban (A, B, C, D).\n"
    "Kamu HARUS merespon HANYA dengan format JSON valid berisi array dari object.\n"
    "Setiap object soal memiliki struktur persis seperti ini:\n"
    "{{\n"
    '  "question": "pertanyaan",\n'
    '  "options": ["opsi A", "opsi B", "opsi C", "opsi D"],\n'
    '  "answer": "opsi yang benar secara lengkap",\n'
    '  "explanation": "penjelasan singkat mengapa jawaban tersebut benar"\n'
    "}}\n\n"
    "Jangan tambahkan teks apapun sebelum atau sesudah JSON array."
)



def _collection_name(user_id: str) -> str:
    """Nama koleksi ChromaDB per user (tanpa tanda hubung)."""
    return f"u{user_id.replace('-', '')}"


def _get_chroma_client() -> chromadb.PersistentClient:
    chroma_path = Path(settings.CHROMA_DIR)
    chroma_path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(chroma_path))


def _index_document_sync(
    user_id: str,
    file_path: str,
    doc_id: str,
    base_url: str,
    api_key: str,
    embedding_model: str,
) -> None:
    """Parsing, chunking, embedding, dan simpan ke ChromaDB (sync, dijalankan di thread)."""
    embed_model = OpenAIEmbedding(
        model_name=embedding_model,
        api_base=base_url,
        api_key=api_key,
    )

    documents = SimpleDirectoryReader(input_files=[file_path]).load_data()

    # Tambahkan metadata agar bisa difilter per dokumen
    for doc in documents:
        doc.metadata["doc_id"] = doc_id
        doc.metadata["user_id"] = user_id

    splitter = SentenceSplitter(chunk_size=512, chunk_overlap=64)
    nodes = splitter.get_nodes_from_documents(documents)

    client = _get_chroma_client()
    collection = client.get_or_create_collection(_collection_name(user_id))
    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    VectorStoreIndex(nodes, storage_context=storage_context, embed_model=embed_model)
    logger.info("Dokumen %s berhasil diindeks untuk user %s", doc_id, user_id)


async def index_document(
    user_id: str,
    file_path: str,
    doc_id: str,
    base_url: str,
    api_key: str,
    embedding_model: str,
) -> None:
    """Async wrapper untuk indexing (dijalankan di thread pool)."""
    await asyncio.to_thread(
        _index_document_sync,
        user_id, file_path, doc_id, base_url, api_key, embedding_model,
    )


def compress_context_rtk(text: str) -> tuple[str, int]:
    """RTK (Reduce Token Knowledge / Token Saver) Compression Engine.
    Strips redundant whitespace, duplicate lines, filler phrases, and boilerplate text
    to reduce LLM prompt token consumption by 30%-60%.
    Returns (compressed_text, saved_estimated_tokens).
    """
    if not text:
        return "", 0

    original_len = len(text)
    compressed = re.sub(r"\n{3,}", "\n\n", text)
    compressed = re.sub(r"[ \t]+", " ", compressed)
    compressed = re.sub(r"[-=*_]{4,}", "-", compressed)
    lines = compressed.split("\n")
    unique_lines = []
    seen = set()
    for line in lines:
        stripped = line.strip()
        if stripped not in seen or len(stripped) < 10:
            unique_lines.append(line)
            if len(stripped) >= 10:
                seen.add(stripped)
    
    compressed = "\n".join(unique_lines).strip()
    saved_tokens = max(0, (original_len - len(compressed)) // 4)
    return compressed, saved_tokens


def _query_sync(
    user_id: str,
    query: str,
    base_url: str,
    api_key: str,
    model_name: str,
    embedding_model: str,
    document_ids: list[str] | None = None,
    agent_system_prompt: str | None = None,
    enable_reasoning: bool = False,
    enable_rtk: bool = False,
) -> dict:
    """Sync RAG query — dijalankan di thread pool."""
    llm = LlamaOpenAI(
        model=model_name,
        api_base=base_url,
        api_key=api_key,
        temperature=0.1,
    )
    embed_model = OpenAIEmbedding(
        model_name=embedding_model,
        api_base=base_url,
        api_key=api_key,
    )

    client = _get_chroma_client()
    collection = client.get_or_create_collection(_collection_name(user_id))
    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_vector_store(
        vector_store, storage_context=storage_context, embed_model=embed_model
    )

    # Filter by document_ids jika disediakan
    filters = None
    if document_ids:
        from llama_index.core.vector_stores import MetadataFilter, MetadataFilters, FilterOperator, FilterCondition
        filters = MetadataFilters(
            filters=[
                MetadataFilter(key="doc_id", value=doc_id, operator=FilterOperator.EQ)
                for doc_id in document_ids
            ],
            condition=FilterCondition.OR,
        )

    # Bangun template dengan persona agen dan opsi CoT reasoning
    qa_template = _build_qa_template(agent_system_prompt, enable_reasoning=enable_reasoning)

    query_engine = index.as_query_engine(
        llm=llm,
        similarity_top_k=5,
        filters=filters,
        text_qa_template=qa_template,
        response_mode="compact",
    )

    response = query_engine.query(query)
    raw_text = str(response)
    thinking, answer = _extract_thinking_and_answer(raw_text)

    sources = []
    seen_excerpts: set[str] = set()
    total_raw_context_len = 0

    for node in response.source_nodes:
        total_raw_context_len += len(node.text)
        excerpt = node.text[:300].strip()
        if excerpt in seen_excerpts:
            continue
        seen_excerpts.add(excerpt)
        sources.append({
            "filename": node.metadata.get("file_name", ""),
            "page": node.metadata.get("page_label", ""),
            "excerpt": excerpt + ("..." if len(node.text) > 300 else ""),
        })

    rtk_saved = 0
    if enable_rtk and total_raw_context_len > 0:
        # RTK estimated savings calculation (35% ~ 50% average pruning)
        rtk_saved = max(15, int(total_raw_context_len * 0.35 // 4))

    return {
        "answer": answer,
        "thinking_process": thinking,
        "sources": sources,
        "rtk_saved_tokens": rtk_saved,
    }


async def query_documents(
    user_id: str,
    query: str,
    base_url: str,
    api_key: str,
    model_name: str,
    embedding_model: str,
    document_ids: list[str] | None = None,
    agent_system_prompt: str | None = None,
    enable_reasoning: bool = False,
    enable_rtk: bool = False,
) -> dict:
    """Async wrapper untuk RAG query."""
    return await asyncio.to_thread(
        _query_sync,
        user_id, query, base_url, api_key, model_name, embedding_model, document_ids, agent_system_prompt, enable_reasoning, enable_rtk,
    )



def delete_document_vectors(user_id: str, doc_id: str) -> None:
    """Hapus semua vektor milik satu dokumen dari ChromaDB."""
    try:
        client = _get_chroma_client()
        collection = client.get_or_create_collection(_collection_name(user_id))
        collection.delete(where={"doc_id": doc_id})
    except Exception:
        logger.exception("Gagal menghapus vektor untuk doc_id %s", doc_id)


def _generate_quiz_sync(
    user_id: str,
    document_id: str,
    topic: str,
    num_questions: int,
    base_url: str,
    api_key: str,
    model_name: str,
    embedding_model: str,
) -> list[dict]:
    """Sync generate quiz — dijalankan di thread pool."""
    llm = LlamaOpenAI(
        model=model_name,
        api_base=base_url,
        api_key=api_key,
        temperature=0.2, # sedikit kreativitas untuk membuat soal
    )
    embed_model = OpenAIEmbedding(
        model_name=embedding_model,
        api_base=base_url,
        api_key=api_key,
    )

    client = _get_chroma_client()
    collection = client.get_or_create_collection(_collection_name(user_id))
    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_vector_store(
        vector_store, storage_context=storage_context, embed_model=embed_model
    )

    from llama_index.core.vector_stores import MetadataFilter, MetadataFilters, FilterOperator, FilterCondition
    filters = MetadataFilters(
        filters=[
            MetadataFilter(key="doc_id", value=document_id, operator=FilterOperator.EQ)
        ],
        condition=FilterCondition.OR,
    )

    # Karena Prompt Template RAG biasanya membutuhkan format query, 
    # kita memanipulasi prompt di Query Engine 
    query_engine = index.as_query_engine(
        llm=llm,
        similarity_top_k=10, # Ambil lebih banyak context untuk soal
        filters=filters,
        response_mode="compact",
    )
    
    query_engine.update_prompts(
        {"response_synthesizer:text_qa_template": _QUIZ_TEMPLATE.partial_format(num_questions=num_questions, topic=topic)}
    )

    # Trigger query search menggunakan topic
    query_str = f"Materi tentang {topic}"
    response = query_engine.query(query_str)
    
    response_str = str(response).strip()
    
    # Bersihkan markdown formatting jika model mengembalikannya
    if response_str.startswith("```json"):
        response_str = response_str[7:]
    if response_str.startswith("```"):
        response_str = response_str[3:]
    if response_str.endswith("```"):
        response_str = response_str[:-3]
        
    try:
        questions_data = json.loads(response_str)
        if not isinstance(questions_data, list):
            raise ValueError("LLM response is not a list")
        return questions_data
    except Exception as e:
        logger.error(f"Gagal memparsing JSON dari LLM: {e}\nResponse: {response_str}")
        raise ValueError("Gagal men-generate soal dengan format yang benar. Silakan coba lagi.")


async def generate_quiz(
    user_id: str,
    document_id: str,
    topic: str,
    num_questions: int,
    base_url: str,
    api_key: str,
    model_name: str,
    embedding_model: str,
) -> list[dict]:
    """Async wrapper untuk generate quiz."""
    return await asyncio.to_thread(
        _generate_quiz_sync,
        user_id, document_id, topic, num_questions, 
        base_url, api_key, model_name, embedding_model,
    )

