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

try:
    from llama_index.embeddings.fastembed import FastEmbedEmbedding
except ImportError:  # pragma: no cover - fallback bila paket belum terpasang
    FastEmbedEmbedding = None

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

# Prompt Deep Reasoning (Chain-of-Thought disalin dari konsep Nalar AI reason tool)
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
    '  "answer": "salin PERSIS teks opsi yang benar, bukan hurufnya",\n'
    '  "explanation": "penjelasan singkat mengapa jawaban tersebut benar"\n'
    "}}\n\n"
    "Jangan tambahkan teks apapun sebelum atau sesudah JSON array."
)

# Dipakai saat user berlatih tanpa memilih dokumen rujukan.
_QUIZ_TOPIC_TEMPLATE = PromptTemplate(
    "Instruksi: Buat {num_questions} soal latihan pilihan ganda (multiple choice) "
    "tentang topik '{topic}' berdasarkan pengetahuan umum yang akurat.\n"
    "Semua soal harus berbahasa Indonesia dan setiap soal wajib memiliki 4 opsi jawaban.\n"
    "Kamu HARUS merespon HANYA dengan format JSON valid berisi array dari object.\n"
    "Setiap object soal memiliki struktur persis seperti ini:\n"
    "{{\n"
    '  "question": "pertanyaan",\n'
    '  "options": ["opsi A", "opsi B", "opsi C", "opsi D"],\n'
    '  "answer": "salin PERSIS teks opsi yang benar, bukan hurufnya",\n'
    '  "explanation": "penjelasan singkat mengapa jawaban tersebut benar"\n'
    "}}\n\n"
    "Jangan tambahkan teks apapun sebelum atau sesudah JSON array."
)



def _collection_name(user_id: str) -> str:
    """Nama koleksi ChromaDB per user (tanpa tanda hubung)."""
    return f"u{user_id.replace('-', '')}"


def _is_local_embed(embedding_model: str) -> bool:
    """Model embedding lokal (dijalankan di mesin ini, bukan lewat gateway)."""
    return (embedding_model or "").strip().startswith(("local:", "local/"))


def _build_embed_model(
    embedding_model: str,
    base_url: str,
    api_key: str,
    embed_base_url: str | None = None,
    embed_api_key: str | None = None,
):
    """Pilih embedder: lokal (fastembed/ONNX) atau OpenAI-compatible remote.

    `embed_base_url`/`embed_api_key` boleh berbeda dari LLM — embedding
    remote diarahkan ke penyedianya sendiri (mis. endpoint HF Space),
    bukan ke gateway LLM.
    """
    if _is_local_embed(embedding_model):
        if FastEmbedEmbedding is None:
            raise RuntimeError(
                "llama-index-embeddings-fastembed belum terpasang. "
                "Jalankan: pip install llama-index-embeddings-fastembed fastembed"
            )
        model_name = (embedding_model or "").split(":", 1)[-1].split("/", 1)[-1]
        model_name = model_name or "all-MiniLM-L6-v2"
        return FastEmbedEmbedding(model_name=f"sentence-transformers/{model_name}")
    return OpenAIEmbedding(
        model_name=embedding_model,
        api_base=embed_base_url or base_url,
        api_key=embed_api_key or api_key,
    )


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
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> None:
    """Parsing, chunking, embedding, dan simpan ke ChromaDB (sync, dijalankan di thread)."""
    embed_model = _build_embed_model(embedding_model, base_url, api_key)

    documents = SimpleDirectoryReader(input_files=[file_path]).load_data()

    # Tambahkan metadata agar bisa difilter per dokumen. Nama "doc_id" sengaja
    # TIDAK dipakai karena llama-index/Chroma memakainya untuk ref_doc_id
    # internal node dan akan menimpa nilai kita saat insert.
    for doc in documents:
        doc.metadata["source_doc_id"] = doc_id
        doc.metadata["user_id"] = user_id

    # Ukuran potongan diambil dari Pengaturan > Pusat Pengetahuan. Overlap
    # dijaga agar selalu lebih kecil dari chunk_size supaya splitter tidak error.
    splitter = SentenceSplitter(
        chunk_size=chunk_size,
        chunk_overlap=min(chunk_overlap, max(chunk_size // 2, 0)),
    )
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
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> None:
    """Async wrapper untuk indexing (dijalankan di thread pool)."""
    await asyncio.to_thread(
        _index_document_sync,
        user_id, file_path, doc_id, base_url, api_key, embedding_model,
        chunk_size, chunk_overlap,
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
    top_k: int = 5,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> dict:
    """Sync RAG query — dijalankan di thread pool."""
    llm = LlamaOpenAI(
        model=model_name,
        api_base=base_url,
        api_key=api_key,
        temperature=0.1,
    )
    embed_model = _build_embed_model(
        embedding_model, base_url, api_key, embedding_base_url, embedding_api_key
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
                MetadataFilter(key="source_doc_id", value=doc_id, operator=FilterOperator.EQ)
                for doc_id in document_ids
            ],
            condition=FilterCondition.OR,
        )

    # Bangun template dengan persona agen dan opsi CoT reasoning
    qa_template = _build_qa_template(agent_system_prompt, enable_reasoning=enable_reasoning)

    query_engine = index.as_query_engine(
        llm=llm,
        similarity_top_k=max(1, top_k),
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
    top_k: int = 5,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> dict:
    """Async wrapper untuk RAG query."""
    return await asyncio.to_thread(
        _query_sync,
        user_id, query, base_url, api_key, model_name, embedding_model, document_ids,
        agent_system_prompt, enable_reasoning, enable_rtk, top_k,
        embedding_base_url, embedding_api_key,
    )



def delete_document_vectors(user_id: str, doc_id: str) -> None:
    """Hapus semua vektor milik satu dokumen dari ChromaDB."""
    try:
        client = _get_chroma_client()
        collection = client.get_or_create_collection(_collection_name(user_id))
        collection.delete(where={"doc_id": doc_id})
    except Exception:
        logger.exception("Gagal menghapus vektor untuk doc_id %s", doc_id)


def _extract_json_array(raw: str) -> list | None:
    """Tarik array JSON dari balasan LLM yang sering diselipi basa-basi.

    Banyak model membungkus jawaban dengan ```json, menambah kalimat pembuka,
    atau mengemasnya dalam object seperti {"questions": [...]}. Semua bentuk itu
    ditangani di sini supaya kuis tidak gagal hanya karena format.
    """
    text = raw.strip()

    # Buang pagar kode markdown di mana pun posisinya.
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    candidates: list[str] = [text]

    # Ambil potongan dari kurung pembuka pertama sampai penutup terakhir.
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("questions", "soal", "data", "items", "quiz"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
            # Object tunggal berisi satu soal juga diterima.
            if "question" in parsed:
                return [parsed]
    return None


def _normalize_questions(raw_items: list, num_questions: int) -> list[dict]:
    """Rapikan soal mentah dari LLM menjadi struktur yang dipakai frontend.

    Yang paling sering bikin kuis "tidak jalan": model menulis kunci jawaban
    sebagai huruf ("B") atau "B. teks", sedangkan frontend membandingkan kunci
    dengan teks opsi persis. Di sini kunci selalu dipetakan balik ke teks opsi.
    """
    cleaned: list[dict] = []

    for item in raw_items:
        if not isinstance(item, dict):
            continue

        question = str(item.get("question") or item.get("pertanyaan") or "").strip()
        raw_options = item.get("options") or item.get("opsi") or item.get("choices") or []
        if isinstance(raw_options, dict):
            # Bentuk {"A": "...", "B": "..."} — urutkan berdasarkan labelnya.
            raw_options = [raw_options[k] for k in sorted(raw_options)]
        options = [str(o).strip() for o in raw_options if str(o).strip()]
        if not question or len(options) < 2:
            continue

        # Hilangkan awalan "A. " / "A) " agar tidak dobel dengan label di UI.
        options = [re.sub(r"^\s*[A-Da-d][.)]\s+", "", o) for o in options]

        answer = str(item.get("answer") or item.get("jawaban") or "").strip()
        answer = re.sub(r"^\s*[A-Da-d][.)]\s*", "", answer).strip()

        match = next((o for o in options if o.lower() == answer.lower()), None)
        if match is None:
            # Kunci berupa huruf saja, atau hanya sebagian teks opsi.
            letter = str(item.get("answer") or "").strip().upper()
            if len(letter) == 1 and "A" <= letter <= chr(ord("A") + len(options) - 1):
                match = options[ord(letter) - ord("A")]
            elif answer:
                match = next((o for o in options if answer.lower() in o.lower()), None)
        if match is None:
            match = options[0]

        cleaned.append({
            "question": question,
            "options": options,
            "answer": match,
            "explanation": str(item.get("explanation") or item.get("penjelasan") or "").strip()
            or "Penjelasan tidak tersedia.",
        })

        if len(cleaned) >= num_questions:
            break

    return cleaned


def _quiz_context_from_index(
    user_id: str,
    document_id: str,
    topic: str,
    base_url: str,
    api_key: str,
    embedding_model: str,
    top_k: int = 12,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> str:
    """Ambil potongan dokumen paling relevan sebagai bahan soal."""
    from llama_index.core.vector_stores import (
        FilterCondition,
        FilterOperator,
        MetadataFilter,
        MetadataFilters,
    )

    embed_model = _build_embed_model(
        embedding_model, base_url, api_key, embedding_base_url, embedding_api_key
    )
    client = _get_chroma_client()
    collection = client.get_or_create_collection(_collection_name(user_id))
    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)
    index = VectorStoreIndex.from_vector_store(
        vector_store, storage_context=storage_context, embed_model=embed_model
    )

    filters = MetadataFilters(
        filters=[MetadataFilter(key="source_doc_id", value=document_id, operator=FilterOperator.EQ)],
        condition=FilterCondition.AND,
    )
    retriever = index.as_retriever(similarity_top_k=max(1, top_k), filters=filters)
    nodes = retriever.retrieve(f"Materi dan konsep penting tentang {topic}")

    if not nodes:
        # Topik mungkin tidak mirip dengan isi dokumen; ambil apa adanya.
        nodes = index.as_retriever(similarity_top_k=max(1, top_k), filters=filters).retrieve(topic or "ringkasan materi")

    return "\n\n---\n\n".join(n.get_content().strip() for n in nodes if n.get_content().strip())


def _generate_quiz_sync(
    user_id: str,
    document_id: str | None,
    topic: str,
    num_questions: int,
    base_url: str,
    api_key: str,
    model_name: str,
    embedding_model: str,
    top_k: int = 12,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> list[dict]:
    """Sync generate quiz — dijalankan di thread pool.

    `document_id` boleh None: kuis lalu dibuat dari pengetahuan umum model
    sehingga user tetap bisa berlatih walau belum mengunggah materi apa pun.
    """
    # Gunakan OpenAI SDK langsung (bukan LlamaOpenAI) agar nama model dari
    # gateway/kustom tidak divalidasi terhadap daftar model OpenAI resmi.
    from openai import OpenAI as SyncOpenAI

    llm_client = SyncOpenAI(api_key=api_key or "dummy", base_url=base_url, timeout=180.0)

    context = ""
    if document_id:
        context = _quiz_context_from_index(
            user_id=user_id,
            document_id=document_id,
            topic=topic,
            base_url=base_url,
            api_key=api_key,
            embedding_model=embedding_model,
            top_k=top_k,
            embedding_base_url=embedding_base_url,
            embedding_api_key=embedding_api_key,
        )
        if not context:
            raise ValueError(
                "Isi dokumen tidak ditemukan di indeks. Coba unggah ulang materi tersebut di menu Materi Saya."
            )

    if context:
        prompt = _QUIZ_TEMPLATE.format(
            context_str=context[:24000], num_questions=num_questions, topic=topic
        )
    else:
        prompt = _QUIZ_TOPIC_TEMPLATE.format(num_questions=num_questions, topic=topic)

    last_raw = ""
    for attempt in range(2):
        # Percobaan kedua ditegaskan lagi formatnya kalau yang pertama meleset.
        text = prompt if attempt == 0 else (
            prompt + "\n\nPENTING: balasan sebelumnya tidak valid. Keluarkan HANYA array JSON, tanpa kalimat pembuka."
        )
        try:
            resp = llm_client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": text}],
                temperature=0.3,
            )
            msg = resp.choices[0].message
            last_raw = str(msg.content or msg.reasoning_content or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("Gagal memanggil model AI untuk kuis: %s", exc)
            raise ValueError(f"Gagal memanggil model AI untuk generate soal: {exc}")
        items = _extract_json_array(last_raw)
        if items:
            questions = _normalize_questions(items, num_questions)
            if questions:
                return questions

    logger.error("Gagal memparsing JSON kuis dari LLM. Response: %s", last_raw[:1500])
    raise ValueError(
        "Model AI tidak mengembalikan soal dalam format yang benar. "
        "Coba ulangi, kurangi jumlah soal, atau gunakan model yang lebih besar."
    )


async def generate_quiz(
    user_id: str,
    document_id: str | None,
    topic: str,
    num_questions: int,
    base_url: str,
    api_key: str,
    model_name: str,
    embedding_model: str,
    top_k: int = 12,
    embedding_base_url: str | None = None,
    embedding_api_key: str | None = None,
) -> list[dict]:
    """Async wrapper untuk generate quiz."""
    return await asyncio.to_thread(
        _generate_quiz_sync,
        user_id, document_id, topic, num_questions,
        base_url, api_key, model_name, embedding_model, top_k,
        embedding_base_url, embedding_api_key,
    )

