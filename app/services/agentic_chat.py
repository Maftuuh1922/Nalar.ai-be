import json
import logging
import uuid
from typing import AsyncGenerator, Any

from sqlalchemy.ext.asyncio import AsyncSession
from openai import AsyncOpenAI

from app.services.document_tools import list_documents, read_document, search_in_document, search_web, DOCUMENT_TOOLS

logger = logging.getLogger(__name__)

async def run_agentic_chat_stream(
    client: AsyncOpenAI,
    model_name: str,
    user_message: str,
    agent_system_prompt: str | None,
    db: AsyncSession,
    user_id: uuid.UUID,
    user_images: list[str] | None = None,
    chat_history: list[dict] | None = None,
    enable_rtk: bool = False
) -> AsyncGenerator[str, None]:
    """
    Menjalankan loop tool-calling agentic untuk merespons pesan user secara streaming.
    Yields JSON string events:
    - {"event": "text", "data": "chunk"}
    - {"event": "tool_call", "name": "...", "args": "..."}
    - {"event": "tool_result", "name": "...", "result": "..."}
    - {"event": "end"}
    """
    messages = []
    
    markdown_instruction = "Gunakan format teks akademik yang rapi dan terstruktur (termasuk tabel jika ada data yang perlu dirangkum/dibandingkan) agar penjelasanmu seperti buku teks atau jurnal. DILARANG KERAS menggunakan emoji atau emoticon (seperti 😊, 📚, dll) dalam seluruh jawabanmu. Pertahankan nada formal dan ilmiah. JIKA kamu membuat tabel perbandingan atau rangkuman, WAJIB tambahkan 'Kesimpulan' singkat di bawah tabel tersebut yang menyoroti inti perbedaannya. JIKA pengguna meminta untuk dibuatkan diagram, struktur, mindmap, atau flowchart, berikan kode XML Draw.io murni di dalam blok kode ````drawio ... ````. Kode XML harus valid, diawali dengan <mxfile> dan diakhiri dengan </mxfile>. PENTING TENTANG DIAGRAM: Gunakan layout yang terstruktur dan luas, jangan sampai node saling bertumpuk (overlap). Beri jarak (spacing) yang jauh antar node (minimal 120px vertikal dan horisontal). Pastikan ukuran (width & height) setiap node cukup besar (misal width=180, height=80) atau disesuaikan otomatis dengan panjang teks (autosize=1). Gunakan panah yang rapi: edgeStyle=orthogonalEdgeStyle;rounded=1;. Gunakan warna profesional dan bedakan warna tiap level/cabang. Jika pengguna memberikan [Context Diagram Draw.io Saat Ini] pada promptnya, PENTING: modifikasi dan kembalikan SELURUH kode XML terbaru secara utuh yang sudah merangkum permintaannya."
    if agent_system_prompt:
        messages.append({"role": "system", "content": f"{agent_system_prompt}\n\n{markdown_instruction}"})
    else:
        messages.append({"role": "system", "content": markdown_instruction})
    
    if chat_history:
        messages.extend(chat_history)
        
    if user_images:
        msg_content = [{"type": "text", "text": user_message}]
        for img in user_images:
            msg_content.append({"type": "image_url", "image_url": {"url": img}})
        messages.append({"role": "user", "content": msg_content})
    else:
        messages.append({"role": "user", "content": user_message})

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
            "temperature": 0.7,
            "stream": True,
            "stream_options": {"include_usage": True}
        }
        
        if not force_answer:
            api_kwargs["tools"] = DOCUMENT_TOOLS
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

            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
                
            # Stream normal text
            if delta.content:
                content_buffer += delta.content
                yield json.dumps({"event": "text", "data": delta.content}) + "\n"
                
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
            # Tidak ada tool calls, berarti jawaban final selesai
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
                    tool_result_str = await search_in_document(db, user_id, **tc_args)
                elif tc_name == "search_web":
                    tool_result_str = await search_web(**tc_args)
                else:
                    tool_result_str = json.dumps({"error": f"Unknown tool: {tc_name}"})
            except Exception as e:
                tool_result_str = json.dumps({"error": f"Tool execution failed: {str(e)}"})

            # Beritahu frontend bahwa tool selesai
            yield json.dumps({"event": "tool_result", "name": tc_name, "result": "Berhasil mendapatkan hasil"}) + "\n"

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result_str
            })

    yield json.dumps({"event": "end"}) + "\n"
