import json
import logging
import re
import uuid
from typing import AsyncGenerator, Any

from sqlalchemy.ext.asyncio import AsyncSession
from openai import AsyncOpenAI

from app.services.document_tools import (
    list_documents,
    read_document,
    search_in_document,
    search_web,
    fetch_webpage,
    arxiv_search,
    rag_query,
    deep_critical_analysis,
    reference_rank,
    canvas_write,
    cite_insert,
    file_export,
    DOCUMENT_TOOLS,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Penyelamat tool-call mentah
#
# Sebagian endpoint (mis. gateway yang membungkus DeepSeek) tidak mengurai
# panggilan tool dan malah mengalirkan markup internal model apa adanya ke
# dalam teks jawaban, seperti:
#
#   <|｜DSML|｜tool_calls><|｜DSML|｜invoke name="search_web"> ...
#
# Kalau dibiarkan, user melihat markup mentah dan alurnya berhenti — diagram
# atau hasil pencarian tidak pernah muncul. Kita kenali polanya, jalankan
# toolnya seperti panggilan normal, dan buang markup itu dari jawaban.
# --------------------------------------------------------------------------

# `｜` (U+FF5C) dan `|` biasa dipakai bergantian oleh model, jumlahnya pun bisa 1-2.
_BAR = r"[|｜]{1,2}"
_DSML_BLOCK = re.compile(rf"<{_BAR}DSML{_BAR}tool_calls>.*?(?:</{_BAR}DSML{_BAR}tool_calls>|\Z)", re.DOTALL)
_DSML_INVOKE = re.compile(
    rf"<{_BAR}DSML{_BAR}invoke\s+name=\"([^\"]+)\"\s*>(.*?)</{_BAR}DSML{_BAR}invoke>", re.DOTALL
)
_DSML_PARAM = re.compile(
    rf"<{_BAR}DSML{_BAR}parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>"
    rf"(.*?)</{_BAR}DSML{_BAR}parameter>",
    re.DOTALL,
)
# Awal markup yang perlu ditahan agar tidak keburu terkirim ke klien.
_DSML_START = re.compile(rf"<{_BAR}DSML")


def _parse_dsml_tool_calls(text: str) -> list[dict[str, Any]]:
    """Ubah markup tool-call mentah menjadi struktur tool_call ala OpenAI."""
    calls: list[dict[str, Any]] = []
    for name, body in _DSML_INVOKE.findall(text):
        args: dict[str, Any] = {}
        for arg_name, is_string, raw in _DSML_PARAM.findall(body):
            value = raw.strip()
            if is_string == "false":
                # Parameter non-string: angka/bool/JSON. Kalau gagal, pakai apa adanya.
                try:
                    value = json.loads(value)
                except Exception:
                    pass
            args[arg_name] = value
        calls.append({
            "id": f"dsml_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


def _strip_dsml(text: str) -> str:
    """Buang seluruh markup tool-call mentah dari teks jawaban.

    Bagian sebelum markup sengaja tidak dipangkas agar posisi karakternya tetap
    sama — penahan streaming memakai indeks itu untuk tahu apa yang sudah dikirim.
    """
    return _DSML_BLOCK.sub("", text)


def _extract_sources(tool_name: str, raw_result: str, tool_args: dict[str, Any]) -> list[dict[str, str]]:
    """Ambil daftar sumber (judul + url/lokasi) dari hasil sebuah tool.

    Dipakai frontend untuk menyusun daftar pustaka otomatis di editor catatan.
    Selalu mengembalikan list — kegagalan parsing tidak boleh menghentikan stream.
    """
    try:
        payload = json.loads(raw_result)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []

    sources: list[dict[str, str]] = []

    if tool_name == "search_web":
        for item in payload.get("results", [])[:10]:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            sources.append({
                "type": "web",
                "title": (item.get("title") or url).strip(),
                "url": url,
                "snippet": (item.get("body") or "").strip()[:300],
            })
    elif tool_name == "fetch_webpage":
        url = (payload.get("url") or tool_args.get("url") or "").strip()
        if url and not payload.get("error"):
            sources.append({
                "type": "web",
                "title": (payload.get("title") or url).strip(),
                "url": url,
                "snippet": (payload.get("text") or "").strip()[:300],
            })
    elif tool_name in ("read_document", "search_in_document"):
        ref = (
            payload.get("filename")
            or payload.get("document")
            or tool_args.get("document_id_or_filename")
            or ""
        )
        if ref:
            sources.append({"type": "document", "title": str(ref), "url": "", "snippet": ""})

    return sources


async def run_agentic_chat_stream(
    client: AsyncOpenAI,
    model_name: str,
    user_message: str,
    agent_system_prompt: str | None,
    db: AsyncSession,
    user_id: uuid.UUID,
    user_images: list[str] | None = None,
    chat_history: list[dict] | None = None,
    enable_rtk: bool = False,
    max_tokens: int = 8000,
    temperature: float = 0.7,
    enable_web_tools: bool = True,
    enable_document_tools: bool = True,
    custom_instructions: str | None = None,
    retrieval_top_k: int = 5,
    capability_tier: str = "tidak_didukung",
    capability: str | None = None,
    capability_config: dict | None = None,
) -> AsyncGenerator[str, None]:
    """
    Menjalankan loop tool-calling agentic untuk merespons pesan user secara streaming.
    Yields JSON string events:
    - {"event": "text", "data": "chunk"}
    - {"event": "tool_call", "name": "...", "args": "..."}
    - {"event": "tool_result", "name": "...", "result": "...", "sources": [...]}
    - {"event": "truncated"}  (jawaban berhenti karena kena batas token)
    - {"event": "end"}
    """
    messages = []
    
    # Diagram dikirim sebagai Mermaid, bukan XML Draw.io. Frontend merender
    # Mermaid secara lokal (components/Mermaid.tsx) sehingga diagram tampil
    # tanpa koneksi internet dan warnanya mengikuti tema aplikasi. XML Draw.io
    # dulu dipakai di sini tapi tidak punya renderer di frontend, jadi hasilnya
    # muncul sebagai teks XML mentah di dalam gelembung chat.
    markdown_instruction = (
        "JIKA pengguna meminta untuk dibuatkan diagram, struktur, mindmap, atau "
        "flowchart, berikan kode Mermaid murni di dalam blok kode ```mermaid ... ```. "
        "Kode harus valid dan diawali deklarasi jenis diagram Mermaid yang sesuai "
        "(`flowchart TD`, `flowchart LR`, `sequenceDiagram`, `mindmap`, `erDiagram`, "
        "`classDiagram`, atau `stateDiagram-v2`). "
        "PENTING TENTANG DIAGRAM: gunakan label yang singkat dan jelas, bungkus teks "
        "yang mengandung spasi atau tanda baca dengan tanda kutip ganda, dan hindari "
        "karakter yang merusak sintaks Mermaid. Susun alur secara berjenjang supaya "
        "mudah dibaca, dan pakai bentuk node yang bermakna: `[...]` untuk proses, "
        "`{...}` untuk keputusan, `([...])` untuk titik awal/akhir. Beri gaya warna "
        "lewat `classDef` dan `class` untuk membedakan tiap level atau cabang. "
        "Jika pengguna memberikan [Context Diagram Mermaid Saat Ini] pada promptnya, "
        "PENTING: modifikasi dan kembalikan SELURUH kode Mermaid terbaru secara utuh "
        "yang sudah merangkum permintaannya."
    )

    # Aturan pemakaian tool. Tanpa ini, model kerap menjawab "maaf, saya tidak
    # bisa mencari di internet" padahal tool pencarian tersedia dan hanya
    # mengembalikan nol hasil pada percobaan pertama.
    tool_instruction = (
        "ATURAN PEMAKAIAN TOOL (WAJIB):\n"
        "- Kamu PUNYA akses internet lewat tool `search_web` dan `fetch_webpage`, "
        "serta akses dokumen user lewat `list_documents`, `read_document`, dan "
        "`search_in_document`. Kamu TIDAK BOLEH bilang tidak punya akses.\n"
        "- Saat user meminta informasi, dokumen, referensi, berita, atau data terbaru, "
        "PANGGIL `search_web` lebih dulu. Jangan menjawab dari ingatan saja.\n"
        "- Kalau hasil pencarian kosong, ULANGI dengan kata kunci berbeda minimal "
        "DUA kali lagi: pakai istilah bahasa Inggris, kata kunci yang lebih umum, "
        "atau tambahkan `filetype:pdf` untuk mencari dokumen.\n"
        "- Buka halaman yang paling menjanjikan dengan `fetch_webpage` supaya isinya "
        "benar-benar dibaca, bukan hanya cuplikan hasil pencarian.\n"
        "- DILARANG membalas dengan permintaan maaf seperti 'saya tidak dapat memenuhi "
        "permintaan' atau 'terkendala keterbatasan akses' sebelum benar-benar mencoba "
        "tool beberapa kali. Kalau setelah beberapa percobaan tetap nihil, sebutkan "
        "kata kunci apa saja yang sudah dicoba, lalu tawarkan sudut pencarian lain.\n"
        "- Selalu sertakan judul dan URL sumber yang kamu pakai."
    )
    # Daftar tool disaring sesuai Pengaturan > Percakapan, dan capability_tier.
    _WEB_TOOL_NAMES = {"search_web", "fetch_webpage", "arxiv_search"}
    _ADVANCED_TOOL_NAMES = {"deep_critical_analysis", "reference_rank", "canvas_write", "cite_insert", "file_export"}
    
    active_tools = []
    if capability_tier in ("agentic_dasar_terverifikasi", "agentic_penuh_terverifikasi"):
        for tool in DOCUMENT_TOOLS:
            name = tool["function"]["name"]
            if name in _ADVANCED_TOOL_NAMES and capability_tier != "agentic_penuh_terverifikasi":
                continue
            if name in _WEB_TOOL_NAMES and enable_web_tools:
                active_tools.append(tool)
            elif name not in _WEB_TOOL_NAMES and enable_document_tools:
                active_tools.append(tool)

    if active_tools:
        base_instruction = f"{markdown_instruction}\n\n{tool_instruction}"
    else:
        base_instruction = markdown_instruction
    if custom_instructions:
        base_instruction = f"{base_instruction}\n\nINSTRUKSI KHUSUS DARI PENGGUNA:\n{custom_instructions}"
    if agent_system_prompt:
        messages.append({"role": "system", "content": f"{agent_system_prompt}\n\n{base_instruction}"})
    else:
        messages.append({"role": "system", "content": base_instruction})
    
    if chat_history:
        messages.extend(chat_history)
        
    if user_images:
        msg_content = [{"type": "text", "text": user_message}]
        for img in user_images:
            msg_content.append({"type": "image_url", "image_url": {"url": img}})
        messages.append({"role": "user", "content": msg_content})
    else:
        messages.append({"role": "user", "content": user_message})

    # Mode capability dari composer (deep_research / deep_question / visualize)
    # — kalau ada, sematkan instruksi mode ke pesan user terakhir supaya model
    # benar-benar menjalankan mode tersebut (sebelumnya capability & config
    # dari start_turn dibuang di WS → semua mode jalan sebagai chat polos).
    if capability or capability_config:
        mode_bits = [f"Mode aktif: {capability}"] if capability else []
        if capability_config:
            try:
                mode_bits.append(
                    "Konfigurasi mode: "
                    + json.dumps(capability_config, ensure_ascii=False)[:1200]
                )
            except (TypeError, ValueError):
                pass
        if capability == "journal":
            mode_bits.append(
                "MODE JURNAL ILMIAH: prioritas utama adalah ketepatan bukti, kebaruan yang dapat dipertahankan, dan keterlacakan sitasi. Bedakan fakta sumber, inferensi, dan usulan. Untuk naskah, gunakan struktur IMRaD/format yang diminta, jangan mengarang hasil, angka, DOI, atau referensi. Jika bukti kurang, buat daftar data yang harus dilengkapi."
            )
        mode_note = "\n".join(mode_bits)
        if isinstance(messages[-1].get("content"), str):
            messages[-1]["content"] += f"\n\n{mode_note}"
        elif isinstance(messages[-1].get("content"), list):
            messages[-1]["content"].append({"type": "text", "text": mode_note})

    documents_read = set()
    
    cumulative_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "rtk_saved_tokens": 0
    }
    
    MAX_ITERATIONS = 15
    for iteration in range(MAX_ITERATIONS):
        # Paksa AI untuk menjawab jika sudah mencapai batas iterasi
        force_answer = iteration >= MAX_ITERATIONS - 2
        if force_answer and iteration == MAX_ITERATIONS - 2:
            messages.append({
                "role": "user",
                "content": "PENTING: Waktu pencarian sudah hampir habis. Kamu tidak diizinkan menggunakan tool lagi. Berikan jawaban akhirmu SEKARANG berdasarkan informasi yang sudah terkumpul sejauh ini."
            })
            
        api_kwargs = {
            "model": model_name,
            "messages": messages,
            # Mode jurnal harus lebih deterministik daripada percakapan umum;
            # ini mengurangi variasi klaim dan format pada model murah.
            "temperature": min(temperature, 0.3) if capability == "journal" else temperature,
            "stream": True,
            # Tanpa batas eksplisit banyak provider memakai default kecil (~1000 token),
            # sehingga laporan panjang terpotong di tengah halaman pertama.
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True}
        }
        
        if not force_answer and active_tools:
            api_kwargs["tools"] = active_tools
            api_kwargs["tool_choice"] = "auto"

        try:
            stream = await client.chat.completions.create(**api_kwargs)
        except Exception as e:
            if iteration == 0:
                yield json.dumps({"event": "error", "data": str(e)}) + "\n"
            else:
                logger.error(f"Error in agentic loop iteration {iteration}: {e}")
            break

        tool_calls = {}
        content_buffer = ""
        reasoning_buffer = ""
        finish_reason = None
        # Penahan agar markup tool-call mentah tidak sempat terkirim ke klien.
        emitted_len = 0
        dsml_seen = False
        HOLD_BACK = 16  # cukup untuk menampung awalan "<|｜DSML|｜" yang terpotong antar-chunk

        async for chunk in stream:
            if hasattr(chunk, 'usage') and chunk.usage:
                usage_data = {
                    "prompt_tokens": getattr(chunk.usage, 'prompt_tokens', 0),
                    "completion_tokens": getattr(chunk.usage, 'completion_tokens', 0),
                    "total_tokens": getattr(chunk.usage, 'total_tokens', 0),
                }
                if enable_rtk and usage_data["prompt_tokens"] > 0:
                    usage_data["rtk_saved_tokens"] = int(usage_data["prompt_tokens"] * 0.35)
                else:
                    usage_data["rtk_saved_tokens"] = 0

                cumulative_usage["prompt_tokens"] += usage_data["prompt_tokens"]
                cumulative_usage["completion_tokens"] += usage_data["completion_tokens"]
                cumulative_usage["total_tokens"] += usage_data["total_tokens"]
                cumulative_usage["rtk_saved_tokens"] += usage_data["rtk_saved_tokens"]

                if cumulative_usage["total_tokens"] > 0:
                    yield json.dumps({"event": "usage", "data": cumulative_usage}) + "\n"

            if chunk.choices and getattr(chunk.choices[0], "finish_reason", None):
                finish_reason = chunk.choices[0].finish_reason

            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
                
            # Stream normal text — ditahan sedikit supaya markup tool-call mentah
            # bisa dikenali sebelum terlanjur tampil di layar user.
            if delta.content:
                content_buffer += delta.content
                if not dsml_seen:
                    marker = _DSML_START.search(content_buffer, max(0, emitted_len))
                    if marker:
                        dsml_seen = True
                        safe_end = marker.start()
                    else:
                        safe_end = max(emitted_len, len(content_buffer) - HOLD_BACK)
                    if safe_end > emitted_len:
                        yield json.dumps({"event": "text", "data": content_buffer[emitted_len:safe_end]}) + "\n"
                        emitted_len = safe_end


            # Deepseek specific reasoning streaming (if supported by model)
            if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                reasoning_buffer += delta.reasoning_content
                # we could yield reasoning chunks, but let's just yield as text with a tag or ignore for now
                # Or yield special reasoning event
                yield json.dumps({"event": "reasoning", "data": delta.reasoning_content}) + "\n"
                
            # Stream tool calls
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.index not in tool_calls:
                        tool_calls[tc.index] = {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name if tc.function and tc.function.name else "",
                                "arguments": tc.function.arguments if tc.function and tc.function.arguments else ""
                            }
                        }
                    else:
                        if tc.function and tc.function.arguments:
                            tool_calls[tc.index]["function"]["arguments"] += tc.function.arguments
                            
        # Endpoint yang tidak mengurai tool-call menaruh markup mentahnya di teks.
        # Ambil alih: jalankan toolnya, dan bersihkan markup dari jawaban.
        if dsml_seen:
            if not tool_calls:
                recovered = _parse_dsml_tool_calls(content_buffer)
                if recovered:
                    logger.info(f"Memulihkan {len(recovered)} tool call dari markup mentah")
                    tool_calls = dict(enumerate(recovered))
                else:
                    logger.warning("Markup tool-call mentah terdeteksi tapi gagal diurai")
            content_buffer = _strip_dsml(content_buffer)

        # Kirim sisa teks yang masih ditahan penahan markup
        if emitted_len < len(content_buffer):
            tail = content_buffer[emitted_len:]
            if tail.strip():
                yield json.dumps({"event": "text", "data": tail}) + "\n"
            emitted_len = len(content_buffer)
        content_buffer = content_buffer.strip()

        # Reconstruct the assistant message to append to history
        assistant_msg = {"role": "assistant"}
        if content_buffer:
            assistant_msg["content"] = content_buffer
        if tool_calls:
            assistant_msg["tool_calls"] = [tc for tc in tool_calls.values()]
        else:
            assistant_msg["content"] = content_buffer or reasoning_buffer or ""

        messages.append(assistant_msg)

        if not tool_calls:
            # Tidak ada tool calls, berarti jawaban final selesai.
            # Beritahu klien bila jawaban terpotong batas token supaya bisa
            # meminta lanjutan (laporan panjang butuh beberapa giliran).
            if finish_reason == "length":
                yield json.dumps({"event": "truncated"}) + "\n"
            break

        # Eksekusi tool calls
        for tc_idx, tc in tool_calls.items():
            tc_id = tc["id"]
            tc_name = tc["function"]["name"]
            tc_args_str = tc["function"]["arguments"]
            
            # Beritahu frontend bahwa tool dipanggil
            yield json.dumps({"event": "tool_call", "name": tc_name, "args": tc_args_str}) + "\n"
            
            try:
                tc_args = json.loads(tc_args_str)
            except Exception:
                tc_args = {}

            logger.info(f"Agent calling tool: {tc_name} with args {tc_args}")
            
            tool_result_str = ""
            try:
                if tc_name == "list_documents":
                    tool_result_str = await list_documents(db, user_id)
                elif tc_name == "read_document":
                    doc_id = tc_args.get("document_id_or_filename", "")
                    if doc_id:
                        documents_read.add(doc_id)
                    tool_result_str = await read_document(db, user_id, **tc_args)
                elif tc_name == "search_in_document":
                    doc_id = tc_args.get("document_id_or_filename", "")
                    if doc_id:
                        documents_read.add(doc_id)
                    # Banyaknya potongan yang diambil mengikuti Pengaturan,
                    # kecuali model sengaja menentukan sendiri.
                    tc_args.setdefault("max_matches", retrieval_top_k)
                    tool_result_str = await search_in_document(db, user_id, **tc_args)
                elif tc_name == "search_web":
                    tc_args.setdefault("max_results", retrieval_top_k)
                    tool_result_str = await search_web(**tc_args)
                elif tc_name == "fetch_webpage":
                    tool_result_str = await fetch_webpage(**tc_args)
                elif tc_name == "arxiv_search":
                    tc_args.setdefault("max_results", retrieval_top_k)
                    tool_result_str = await arxiv_search(**tc_args)
                elif tc_name == "rag_query":
                    tc_args.setdefault("max_matches", retrieval_top_k)
                    tool_result_str = await rag_query(db, user_id, **tc_args)
                elif tc_name == "deep_critical_analysis":
                    tool_result_str = await deep_critical_analysis(**tc_args)
                elif tc_name == "reference_rank":
                    tool_result_str = await reference_rank(**tc_args)
                elif tc_name == "canvas_write":
                    tool_result_str = await canvas_write(**tc_args)
                elif tc_name == "cite_insert":
                    tool_result_str = await cite_insert(**tc_args)
                elif tc_name == "file_export":
                    tool_result_str = await file_export(**tc_args)
                else:
                    tool_result_str = json.dumps({"error": f"Unknown tool: {tc_name}"})
            except Exception as e:
                tool_result_str = json.dumps({"error": f"Tool execution failed: {str(e)}"})

            # Beritahu frontend bahwa tool selesai, sekaligus kirim daftar sumber
            # (judul + URL) supaya editor catatan bisa menyusun daftar pustaka.
            yield json.dumps({
                "event": "tool_result",
                "name": tc_name,
                "result": "Berhasil mendapatkan hasil",
                "sources": _extract_sources(tc_name, tool_result_str, tc_args),
            }) + "\n"

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result_str
            })

    yield json.dumps({"event": "end"}) + "\n"
