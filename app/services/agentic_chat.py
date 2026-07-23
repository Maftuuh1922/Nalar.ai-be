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
    user_id: uuid.UUID
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
    if agent_system_prompt:
        messages.append({"role": "system", "content": agent_system_prompt})
    messages.append({"role": "user", "content": user_message})

    documents_read = set()
    
    for iteration in range(6):
        try:
            stream = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=DOCUMENT_TOOLS,
                tool_choice="auto",
                temperature=0.7,
                stream=True
            )
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
