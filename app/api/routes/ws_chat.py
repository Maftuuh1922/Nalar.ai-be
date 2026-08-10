"""WebSocket ChatOrchestrator untuk kolom chat di web.

Frontend (``lib/unified-ws.ts``) mengirim pesan lewat ``/api/v1/ws`` dan
menunggu rangkaian ``StreamEvent``. Handler ini menjalankan
``run_agentic_chat_stream`` yang sudah ada, dengan dua hal penting:

* model yang dipakai diresolusi dari ``llm_selection`` per pesan
  (lihat ``app/services/model_selection.py``), sehingga pemilih model di kolom
  chat benar-benar menentukan model yang dipanggil;
* setiap turn disimpan di ``TurnBroker`` beserta nomor urut event, sehingga
  ``subscribe_turn``/``resume_from`` bisa mengirim ulang bagian yang terlewat
  setelah koneksi terputus — jawaban tidak lagi hilang saat WebSocket mati di
  tengah streaming.

Batasan: ``TurnBroker`` bersifat **in-memory** dan hanya tersedia pada proses
yang sama yang melayani turn tersebut.  Jika backend dijalankan dengan beberapa
worker (``--workers N > 1``) atau proses berhenti, data turn yang belum selesai
akan hilang dan ``resume_from`` tidak bisa mengirimkan kembali event.  Untuk
deploy produksi gunakan satu worker atau pindahkan TurnBroker ke Redis.
"""

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass, field
import io
import json
import logging
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import TOKEN_COOKIE
from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.models.chat_history import ChatHistory
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.agentic_chat import run_agentic_chat_stream
from app.services.model_selection import ModelSelectionError, resolve_llm
from app.services.preferences import build_http_client, get_preferences

logger = logging.getLogger(__name__)

router = APIRouter()

# Batas penyangga replay. Satu turn panjang bisa menghasilkan ribuan potongan
# teks; menyimpannya tanpa batas akan menggerogoti memori proses backend.
MAX_EVENTS_PER_TURN = 4000
MAX_RETAINED_TURNS = 32


def _now_ms() -> int:
    return int(time.time() * 1000)


class Connection:
    """Satu koneksi WebSocket dengan satu penulis (agar frame tidak bertabrakan)."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.user_id: uuid.UUID | None = None
        self.alive = True
        self._lock = asyncio.Lock()

    async def send(self, payload: dict) -> bool:
        """Kirim satu frame. ``False`` berarti socket sudah tidak bisa dipakai."""
        if not self.alive:
            return False
        try:
            async with self._lock:
                await self.websocket.send_text(json.dumps(payload))
        except Exception:  # noqa: BLE001 — socket mati/ditutup di tengah pengiriman
            self.alive = False
            return False
        return True


@dataclass
class TurnState:
    """Riwayat satu turn: status, event ber-nomor urut, dan pelanggannya."""

    turn_id: str
    user_id: uuid.UUID
    session_id: str | None = None
    status: str = "running"
    seq: int = 0
    events: list[dict] = field(default_factory=list)
    first_stored_seq: int = 0
    dropped: int = 0
    subscribers: set[Connection] = field(default_factory=set)
    task: asyncio.Task | None = None
    finished_at_ms: int | None = None

    @property
    def finished(self) -> bool:
        return self.status != "running"


class TurnBroker:
    """Penyimpan turn di memori proses, dengan batas jumlah turn yang disimpan."""

    def __init__(self) -> None:
        self._turns: "OrderedDict[str, TurnState]" = OrderedDict()

    def create(self, user_id: uuid.UUID, session_id: str | None = None) -> TurnState:
        turn = TurnState(turn_id=str(uuid.uuid4()), user_id=user_id, session_id=session_id)
        self._turns[turn.turn_id] = turn
        self._evict()
        return turn

    def get(self, turn_id: str) -> TurnState | None:
        return self._turns.get(turn_id)

    def running_for_session(self, session_id: str, user_id: uuid.UUID) -> TurnState | None:
        for turn in reversed(self._turns.values()):
            if turn.session_id == session_id and turn.user_id == user_id and not turn.finished:
                return turn
        return None

    def latest_for_session(self, session_id: str, user_id: uuid.UUID) -> TurnState | None:
        for turn in reversed(self._turns.values()):
            if turn.session_id == session_id and turn.user_id == user_id:
                return turn
        return None

    def _evict(self) -> None:
        while len(self._turns) > MAX_RETAINED_TURNS:
            for turn_id, turn in self._turns.items():
                if turn.finished:
                    del self._turns[turn_id]
                    break
            else:
                # Semua turn masih berjalan — jangan buang apa pun.
                return

    def subscribe(self, turn: TurnState, conn: Connection) -> None:
        turn.subscribers.add(conn)

    def unsubscribe(self, conn: Connection, *, turn_id: str | None = None, session_id: str | None = None) -> None:
        for turn in self._turns.values():
            if turn_id and turn.turn_id != turn_id:
                continue
            if session_id and turn.session_id != session_id:
                continue
            turn.subscribers.discard(conn)

    def drop_connection(self, conn: Connection) -> None:
        for turn in self._turns.values():
            turn.subscribers.discard(conn)

    async def publish(self, turn: TurnState, event: dict) -> None:
        """Beri nomor urut, simpan untuk replay, lalu kirim ke semua pelanggan."""
        turn.seq += 1
        event["seq"] = turn.seq
        event["turn_id"] = turn.turn_id
        event.setdefault("session_id", turn.session_id)
        event.setdefault("timestamp", _now_ms())
        # Frontend butuh timestamp konsisten (detik epoch) untuk ThoughtTimer
        # dan penanda aktif. Dijamin selalu ada sejak awal — tidak lagi
        # bergantung pada siapa pembuat event.
        event.setdefault("ts", event["timestamp"] / 1000)

        if len(turn.events) < MAX_EVENTS_PER_TURN:
            if not turn.events:
                turn.first_stored_seq = turn.seq
            turn.events.append(event)
        else:
            turn.dropped += 1

        for conn in list(turn.subscribers):
            if not await conn.send(event):
                turn.subscribers.discard(conn)

    async def replay(self, turn: TurnState, conn: Connection, after_seq: int) -> None:
        """Kirim ulang event turn yang nomor urutnya di atas ``after_seq``."""
        if turn.dropped and after_seq < turn.first_stored_seq:
            await conn.send(
                {
                    "type": "observation",
                    "source": "chat",
                    "stage": "chat",
                    "content": (
                        "Sebagian awal jawaban tidak bisa dikirim ulang karena "
                        "penyangga sudah penuh."
                    ),
                    "metadata": {"replay_incomplete": True},
                    "session_id": turn.session_id,
                    "turn_id": turn.turn_id,
                    "seq": after_seq,
                    "timestamp": _now_ms(),
                }
            )
        for event in list(turn.events):
            if event.get("seq", 0) <= after_seq:
                continue
            if not await conn.send(event):
                return


broker = TurnBroker()


class TurnEmitter:
    """Pembungkus tipis supaya kode turn tidak perlu mengurus bentuk event."""

    def __init__(self, turn: TurnState) -> None:
        self._turn = turn

    async def event(
        self,
        event_type: str,
        *,
        content: str = "",
        stage: str = "chat",
        source: str = "chat",
        metadata: dict | None = None,
        turn_id: str | None = None,
    ) -> None:
        await broker.publish(
            self._turn,
            {
                "type": event_type,
                "source": source,
                "stage": stage,
                "content": content,
                "metadata": metadata or {},
                "timestamp": _now_ms(),
                "turn_id": turn_id or self._turn.turn_id,
            },
        )

    async def finish(self, status: str, metadata: dict | None = None) -> None:
        self._turn.status = status
        self._turn.finished_at_ms = _now_ms()
        payload = {"status": status}
        payload.update(metadata or {})
        await self.event("done", metadata=payload)

    async def terminal_error(self, message: str, *, reason: str | None = None) -> None:
        """Error yang mengakhiri turn; ``done`` ikut dikirim agar UI tidak menggantung."""
        metadata: dict = {"turn_terminal": True, "status": "failed"}
        if reason:
            metadata["reason"] = reason
        await self.event("error", content=message, metadata=metadata)
        await self.finish("failed", {"reason": reason} if reason else None)


async def _resolve_ws_user(websocket: WebSocket, db: AsyncSession) -> User | None:
    """Ambil user dari cookie yang ikut pada handshake WebSocket."""
    token = websocket.cookies.get(TOKEN_COOKIE)
    if not token:
        return None
    subject = decode_access_token(token)
    if subject is None:
        return None
    try:
        user_id = uuid.UUID(subject)
    except ValueError:
        return None
    return await db.get(User, user_id)


def _images_from_attachments(payload: dict) -> list[str] | None:
    """Ubah lampiran gambar dari FE menjadi daftar URL/data-URL."""
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        return None

    images: list[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime_type") or "")
        kind = str(item.get("type") or "")
        if kind != "image" and not mime.startswith("image/"):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            images.append(url)
            continue
        base64_data = str(item.get("base64") or "").strip()
        if base64_data:
            if base64_data.startswith("data:"):
                images.append(base64_data)
            else:
                images.append(f"data:{mime or 'image/png'};base64,{base64_data}")
    return images or None


_DOC_CHAR_LIMIT = 40_000
_DOC_TOTAL_CHAR_LIMIT = 120_000


def _decode_attachment(raw_b64: str) -> bytes:
    if raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    return base64.b64decode(raw_b64)


def _extract_pdf_text(raw: bytes) -> str:
    import fitz  # PyMuPDF

    with fitz.open(stream=raw, filetype="pdf") as pdf:
        return "\n".join(page.get_text() for page in pdf).strip()


def _extract_docx_text(raw: bytes) -> str:
    import docx  # python-docx

    doc = docx.Document(io.BytesIO(raw))
    parts: list[str] = []
    for paragraph in doc.paragraphs:
        if paragraph.text.strip():
            parts.append(paragraph.text)
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip(" |"):
                parts.append(row_text)
    return "\n".join(parts).strip()


def _extract_document_texts(payload: dict) -> list[str]:
    """Ekstrak teks dari lampiran dokumen (PDF/DOCX) untuk disertakan ke prompt.

    FE mengirim file sebagai ``base64`` + ``mime_type``/``filename``; dokumen
    tidak diproses sebagai gambar, jadi teksnya diekstrak di sini (PyMuPDF /
    python-docx) dan menjadi blok konteks pesan user.
    """
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        return []

    docs: list[str] = []
    total_chars = 0
    for item in attachments:
        if not isinstance(item, dict):
            continue
        mime = str(item.get("mime_type") or "").lower()
        filename = str(item.get("filename") or "").strip()
        name_lower = filename.lower()
        if "pdf" in mime or name_lower.endswith(".pdf"):
            extractor = _extract_pdf_text
            missing = "PyMuPDF"
        elif (
            "wordprocessingml" in mime
            or mime == "application/msword"
            or name_lower.endswith(".docx")
        ):
            extractor = _extract_docx_text
            missing = "python-docx"
        else:
            continue
        base64_data = str(item.get("base64") or "").strip()
        if not base64_data:
            continue
        try:
            raw = _decode_attachment(base64_data)
            text = extractor(raw)
        except ImportError:
            logger.warning("%s tidak terpasang; lampiran dokumen dilewati", missing)
            break
        except Exception:
            logger.exception("Gagal mengekstrak teks dari lampiran %s", filename or "dokumen")
            continue
        if not text:
            continue
        remaining = _DOC_TOTAL_CHAR_LIMIT - total_chars
        if remaining <= 0:
            break
        if len(text) > _DOC_CHAR_LIMIT:
            text = text[:_DOC_CHAR_LIMIT] + "\n[... teks terpotong ...]"
        if len(text) > remaining:
            text = text[:remaining] + "\n[... teks terpotong ...]"
        total_chars += len(text)
        docs.append(f"[{filename or 'dokumen'}] {text}")
    return docs


def _user_message_with_documents(plan: "TurnPlan") -> str:
    """Pesan user untuk model: lampiran dokumen dulu, pertanyaan terakhir."""
    if not plan.documents_text:
        return plan.content
    blocks = [
        f"[Lampiran dokumen {i}]\n{text}"
        for i, text in enumerate(plan.documents_text, start=1)
    ]
    blocks.append(f"[Pertanyaan]\n{plan.content}")
    return "\n\n".join(blocks)


def _history_entry(record: ChatHistory) -> dict:
    """Satu baris riwayat dalam bentuk pesan chat completions."""
    images_json = getattr(record, "images_json", None)
    if not images_json:
        return {"role": record.role, "content": record.content}
    try:
        images = json.loads(images_json)
    except (TypeError, ValueError):
        return {"role": record.role, "content": record.content}
    parts: list[dict] = [{"type": "text", "text": record.content}]
    for image in images:
        parts.append({"type": "image_url", "image_url": {"url": image}})
    return {"role": record.role, "content": parts}


async def _session_records(db: AsyncSession, session_id) -> list[ChatHistory]:
    records = await db.scalars(
        select(ChatHistory)
        .where(ChatHistory.session_id == session_id)
        .order_by(ChatHistory.created_at.asc())
    )
    return list(records.all())


async def _load_history(db: AsyncSession, session_id, limit: int) -> list[dict]:
    """Riwayat percakapan untuk konteks model, dibatasi seperti alur REST."""
    if not session_id or limit <= 0:
        return []
    return [_history_entry(record) for record in (await _session_records(db, session_id))[-limit:]]


@dataclass
class TurnPlan:
    """Semua bahan yang dibutuhkan untuk menjalankan satu turn."""

    session: ChatSession
    content: str
    is_new_session: bool
    history: list[dict]
    images: list[str] | None = None
    documents_text: list[str] = field(default_factory=list)
    persist_user_message: bool = True
    persona_name: str | None = None
    capability: str | None = None
    config: dict | None = None


async def _translate_and_stream(emitter: TurnEmitter, generator) -> tuple[str, dict | None, bool]:
    """Terjemahkan event NDJSON agentic_chat menjadi StreamEvent WebSocket.

    Returns:
        (answer, usage, had_error) — ``had_error`` bernilai True bila
        generator mengirim event ``error`` (mis. koneksi model terputus).
    """
    answer = ""
    usage: dict | None = None
    had_error = False

    async for chunk in generator:
        try:
            event = json.loads(chunk.strip())
        except (TypeError, ValueError):
            continue

        name = event.get("event")
        if name == "text":
            data = event.get("data") or ""
            answer += data
            await emitter.event("content", content=data)
        elif name == "reasoning":
            await emitter.event("thinking", content=event.get("data") or "")
        elif name == "tool_call":
            tool_name = event.get("name") or ""
            await emitter.event(
                "tool_call",
                content=tool_name,
                stage="tool",
                metadata={"name": tool_name, "args": event.get("args") or ""},
            )
        elif name == "tool_result":
            tool_name = event.get("name") or ""
            await emitter.event(
                "tool_result",
                content=str(event.get("result") or "")[:4000],
                stage="tool",
                metadata={"name": tool_name, "sources": event.get("sources") or []},
            )
            sources = event.get("sources")
            if sources:
                await emitter.event("sources", metadata={"sources": sources})
        elif name == "usage":
            data = event.get("data")
            if isinstance(data, dict):
                usage = data
        elif name == "truncated":
            await emitter.event(
                "observation", content="Jawaban terpotong karena mencapai batas token."
            )
        elif name == "error":
            had_error = True
            await emitter.event(
                "error", content=str(event.get("data") or "Terjadi kesalahan pada model.")
            )
        elif name == "end":
            break

    return answer, usage, had_error


async def _execute_turn(
    turn: TurnState,
    db: AsyncSession,
    user: User,
    plan: TurnPlan,
    selection: object,
) -> None:
    """Jalankan satu turn: resolusi model, streaming, lalu simpan hasilnya."""
    emitter = TurnEmitter(turn)

    try:
        llm = await resolve_llm(db, user.id, selection)
    except ModelSelectionError as exc:
        await emitter.terminal_error(str(exc), reason="model_unavailable")
        return

    prefs = await get_preferences(db, user.id)

    # Jika model tidak mendukung vision tapi ada lampiran gambar, buang
    # gambar agar model tidak error, dan beri tahu user.
    model_caps: list[str] = []
    try:
        model_caps = json.loads(getattr(llm, "capabilities", None) or "[]")
    except Exception:
        model_caps = []
    if plan.images and "vision" not in model_caps:
        stripped_names = []
        for img_url in plan.images:
            if img_url.startswith("data:"):
                stripped_names.append(img_url.split(";")[0].split(":")[1])
            else:
                stripped_names.append(img_url.split("/")[-1].split("?")[0] or "gambar")
        plan = TurnPlan(
            session=plan.session,
            content=plan.content,
            is_new_session=plan.is_new_session,
            history=plan.history,
            images=None,
            documents_text=plan.documents_text,
            persist_user_message=plan.persist_user_message,
            persona_name=plan.persona_name,
            capability=plan.capability,
            config=plan.config,
        )
        await emitter.event(
            "error",
            content=(
                f"\u26a0\ufe0f Model yang aktif tidak mendukung input gambar "
                f"({', '.join(stripped_names)}). "
                "Gambar dilewati \u2014 teks pertanyaan tetap diproses."
            ),
        )

    turn.session_id = str(plan.session.id)
    await emitter.event(
        "session", metadata={"session_id": turn.session_id, "turn_id": turn.turn_id}
    )

    user_msg_id = None
    if plan.persist_user_message:
        user_msg = ChatHistory(
            user_id=user.id,
            session_id=plan.session.id,
            role="user",
            content=plan.content,
            images_json=json.dumps(plan.images) if plan.images else None,
        )
        db.add(user_msg)
        await db.commit()
        await db.refresh(user_msg)
        user_msg_id = str(user_msg.id)

    proxy_client = build_http_client(prefs)
    client = AsyncOpenAI(
        api_key=llm.api_key or "dummy",
        base_url=llm.base_url,
        timeout=float(prefs.request_timeout),
        http_client=proxy_client,
    )

    agent_system_prompt = None
    if plan.persona_name:
        from app.services.preset_loader import get_builtin_personas
        presets = get_builtin_personas()
        for p in presets:
            if p.name == plan.persona_name:
                agent_system_prompt = p.system_prompt
                break
        
        if not agent_system_prompt:
            from app.models.agent import Agent
            agent = await db.scalar(
                select(Agent).where(Agent.name == plan.persona_name, Agent.user_id == user.id)
            )
            if agent:
                agent_system_prompt = agent.system_prompt

    answer = ""
    usage: dict | None = None
    had_error = False
    status = "completed"
    assistant_msg_id = None
    try:
        answer, usage, had_error = await _translate_and_stream(
            emitter,
            run_agentic_chat_stream(
                client=client,
                model_name=llm.model_name,
                user_message=_user_message_with_documents(plan),
                user_images=plan.images,
                agent_system_prompt=agent_system_prompt,
                db=db,
                user_id=user.id,
                chat_history=plan.history,
                max_tokens=prefs.chat_max_tokens,
                temperature=prefs.chat_temperature,
                enable_web_tools=prefs.enable_web_tools,
                enable_document_tools=prefs.enable_document_tools,
                custom_instructions=prefs.custom_instructions,
                retrieval_top_k=prefs.retrieval_top_k,
                capability_tier=llm.capability_tier,
                # Capability yang dipilih user di composer (mis. deep_research)
                # ikut diteruskan agar prompt agentic menyesuaikan mode itu.
                capability=plan.capability,
                capability_config=plan.config,
            ),
        )
    except asyncio.CancelledError:
        status = "cancelled"
    except Exception as exc:  # noqa: BLE001 — kegagalan model dilaporkan ke UI
        logger.exception("Turn chat gagal")
        status = "failed"
        await emitter.event("error", content=f"Gagal menghubungi model AI: {exc}")
    else:
        if had_error:
            status = "failed"
    finally:
        if answer or status == "completed":
            assistant_msg = ChatHistory(
                user_id=user.id,
                session_id=plan.session.id,
                role="assistant",
                content=answer,
                usage_json=json.dumps(usage) if usage else None,
            )
            db.add(assistant_msg)
            await db.commit()
            await db.refresh(assistant_msg)
            assistant_msg_id = str(assistant_msg.id)
        else:
            await db.commit()
            
        if proxy_client is not None:
            await proxy_client.aclose()

    if plan.is_new_session:
        # Memicu sidebar memuat ulang daftar sesi agar sesi baru muncul.
        await emitter.event("session_meta", metadata={"title": plan.session.title})

    await emitter.finish(status, metadata={
        "user_message_id": user_msg_id,
        "assistant_message_id": assistant_msg_id
    })


async def _run_message_turn(turn: TurnState, payload: dict) -> None:
    """Turn dari pesan baru pengguna."""
    emitter = TurnEmitter(turn)
    content = str(payload.get("content") or "").strip()

    async with AsyncSessionLocal() as db:
        user = await db.get(User, turn.user_id)
        if user is None:
            await emitter.terminal_error("Sesi login sudah berakhir. Silakan masuk kembali.")
            return
        if not content:
            await emitter.terminal_error("Pesan kosong.")
            return

        session, is_new = await _resolve_session(db, user, payload.get("session_id"), content)
        prefs = await get_preferences(db, user.id)
        plan = TurnPlan(
            session=session,
            content=content,
            is_new_session=is_new,
            history=await _load_history(db, session.id, max(0, prefs.history_limit)),
            images=_images_from_attachments(payload),
            documents_text=await asyncio.to_thread(_extract_document_texts, payload),
            persona_name=payload.get("persona"),
            capability=str(payload.get("capability") or "") or None,
            config=payload.get("config")
            if isinstance(payload.get("config"), dict)
            else None,
        )
        await _execute_turn(turn, db, user, plan, payload.get("llm_selection"))


async def _run_regenerate_turn(turn: TurnState, payload: dict) -> None:
    """Ulangi jawaban terakhir: buang jawaban lama, jalankan ulang pesan user terakhir."""
    emitter = TurnEmitter(turn)
    raw_session_id = str(payload.get("session_id") or "").strip()
    overrides = payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {}

    async with AsyncSessionLocal() as db:
        user = await db.get(User, turn.user_id)
        if user is None:
            await emitter.terminal_error("Sesi login sudah berakhir. Silakan masuk kembali.")
            return

        session = await _lookup_session(db, user, raw_session_id)
        if session is None:
            await emitter.terminal_error(
                "Sesi chat tidak ditemukan.", reason="nothing_to_regenerate"
            )
            return

        records = await _session_records(db, session.id)
        last_user_index = next(
            (i for i in range(len(records) - 1, -1, -1) if records[i].role == "user"), None
        )
        if last_user_index is None:
            await emitter.terminal_error(
                "Belum ada pesan yang bisa diulang.", reason="nothing_to_regenerate"
            )
            return

        # Jawaban lama (dan apa pun setelah pesan user terakhir) dihapus supaya
        # riwayat tidak menumpuk dua jawaban untuk satu pertanyaan.
        for stale in records[last_user_index + 1 :]:
            await db.delete(stale)
        await db.commit()

        user_record = records[last_user_index]
        try:
            images = json.loads(user_record.images_json) if user_record.images_json else None
        except (TypeError, ValueError):
            images = None

        prefs = await get_preferences(db, user.id)
        history_window = max(0, prefs.history_limit)
        history = [_history_entry(r) for r in records[:last_user_index][-history_window:]] if history_window else []

        plan = TurnPlan(
            session=session,
            content=user_record.content,
            is_new_session=False,
            history=history,
            images=images if isinstance(images, list) else None,
            persist_user_message=False,
            persona_name=overrides.get("persona"),
        )
        await _execute_turn(turn, db, user, plan, overrides.get("llm_selection"))


async def _lookup_session(db: AsyncSession, user: User, raw_session_id: str) -> ChatSession | None:
    if not raw_session_id:
        return None
    try:
        session_uuid = uuid.UUID(raw_session_id)
    except ValueError:
        return None
    return await db.scalar(
        select(ChatSession).where(
            ChatSession.id == session_uuid,
            ChatSession.user_id == user.id,
        )
    )


async def _resolve_session(
    db: AsyncSession, user: User, raw_session_id: object, content: str
) -> tuple[ChatSession, bool]:
    session = await _lookup_session(db, user, str(raw_session_id or "").strip())
    if session is not None:
        return session, False

    session = ChatSession(
        user_id=user.id,
        title=content[:50] + ("..." if len(content) > 50 else ""),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session, True


async def _reject(conn: Connection, message: str, *, reason: str | None = None, turn_id: str | None = None) -> None:
    """Tolak permintaan yang tidak punya turn di server (tanpa mengarang turn baru)."""
    metadata: dict = {"turn_terminal": True, "status": "failed"}
    if reason:
        metadata["reason"] = reason
    await conn.send(
        {
            "type": "error",
            "source": "chat",
            "stage": "chat",
            "content": message,
            "metadata": metadata,
            "session_id": None,
            "turn_id": turn_id,
            "seq": 0,
            "timestamp": _now_ms(),
        }
    )
    await conn.send(
        {
            "type": "done",
            "source": "chat",
            "stage": "chat",
            "content": "",
            "metadata": {"status": "failed", **({"reason": reason} if reason else {})},
            "session_id": None,
            "turn_id": turn_id,
            "seq": 0,
            "timestamp": _now_ms(),
        }
    )


async def _handle_subscribe(conn: Connection, payload: dict, *, by_session: bool) -> None:
    """Sambungkan koneksi ini ke turn yang sudah ada dan kirim bagian terlewat."""
    if conn.user_id is None:
        await _reject(conn, "Sesi login sudah berakhir. Silakan masuk kembali.")
        return

    after_seq = payload.get("after_seq")
    if after_seq is None:
        after_seq = payload.get("seq")
    try:
        after_seq = int(after_seq or 0)
    except (TypeError, ValueError):
        after_seq = 0

    if by_session:
        session_id = str(payload.get("session_id") or "").strip()
        turn = broker.latest_for_session(session_id, conn.user_id) if session_id else None
    else:
        turn_id = str(payload.get("turn_id") or "").strip()
        turn = broker.get(turn_id) if turn_id else None

    if turn is None or turn.user_id != conn.user_id:
        # Turn sudah tidak ada di memori (mis. backend baru di-restart). Kirim
        # penutup supaya UI tidak menunggu jawaban yang tidak akan datang;
        # transkrip tetap bisa dimuat ulang dari database.
        await _reject(
            conn,
            "Percakapan itu sudah tidak bisa dilanjutkan. Muat ulang halaman untuk melihat riwayatnya.",
            reason="turn_not_found",
            turn_id=str(payload.get("turn_id") or "") or None,
        )
        return

    broker.subscribe(turn, conn)
    await broker.replay(turn, conn, after_seq)


@router.websocket("/ws")
async def chat_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    conn = Connection(websocket)

    async with AsyncSessionLocal() as db:
        user = await _resolve_ws_user(websocket, db)
    conn.user_id = user.id if user is not None else None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue

            message_type = str(payload.get("type") or "")

            if message_type == "ping":
                await conn.send({"type": "pong"})
                continue

            if message_type in ("message", "start_turn", "regenerate"):
                if conn.user_id is None:
                    await _reject(conn, "Sesi login sudah berakhir. Silakan masuk kembali.")
                    continue

                session_id = str(payload.get("session_id") or "").strip()
                if session_id and broker.running_for_session(session_id, conn.user_id) is not None:
                    await _reject(
                        conn,
                        "Masih ada jawaban yang sedang berjalan di sesi ini.",
                        reason="regenerate_busy" if message_type == "regenerate" else None,
                    )
                    continue

                turn = broker.create(conn.user_id, session_id or None)
                broker.subscribe(turn, conn)
                runner = _run_regenerate_turn if message_type == "regenerate" else _run_message_turn
                turn.task = asyncio.create_task(_guarded_turn(turn, runner, payload))
                continue

            if message_type in ("subscribe_turn", "resume_from"):
                await _handle_subscribe(conn, payload, by_session=False)
                continue

            if message_type == "subscribe_session":
                await _handle_subscribe(conn, payload, by_session=True)
                continue

            if message_type == "unsubscribe":
                broker.unsubscribe(
                    conn,
                    turn_id=str(payload.get("turn_id") or "") or None,
                    session_id=str(payload.get("session_id") or "") or None,
                )
                continue

            if message_type == "cancel_turn":
                turn_id = str(payload.get("turn_id") or "").strip()
                turn = broker.get(turn_id) if turn_id else None
                if turn is None or turn.user_id != conn.user_id:
                    await _reject(
                        conn, "Turn tidak ditemukan.", reason="turn_not_found", turn_id=turn_id or None
                    )
                    continue
                if turn.task is not None and not turn.task.done():
                    turn.task.cancel()
                continue

            if message_type == "submit_user_reply":
                # Backend Nalar AI tidak punya tool ``ask_user``, jadi tidak ada
                # turn yang menunggu jawaban untuk dilanjutkan.
                await _reject(
                    conn,
                    "Percakapan ini tidak sedang menunggu jawabanmu.",
                    reason="not_waiting_for_reply",
                    turn_id=str(payload.get("turn_id") or "") or None,
                )
                continue

    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — koneksi tetap ditutup rapi
        logger.exception("WebSocket chat berhenti karena error")
    finally:
        conn.alive = False
        broker.drop_connection(conn)


async def _guarded_turn(turn: TurnState, runner, payload: dict) -> None:
    """Pastikan setiap turn selalu berakhir dengan ``done``, apa pun yang terjadi."""
    try:
        await runner(turn, payload)
    except asyncio.CancelledError:
        if not turn.finished:
            await TurnEmitter(turn).finish("cancelled")
    except Exception:  # noqa: BLE001 — jangan sampai task mati tanpa menutup turn
        logger.exception("Turn chat berhenti tak terduga")
        if not turn.finished:
            await TurnEmitter(turn).terminal_error("Terjadi kesalahan internal pada server.")
    finally:
        if not turn.finished:
            await TurnEmitter(turn).finish("failed")
