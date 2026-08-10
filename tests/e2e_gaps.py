"""E2E celah: reconnect/resume, regenerate, cancel, status per user, normalisasi.

Test ini membutuhkan:
- Backend FastAPI berjalan di ``http://127.0.0.1:8080``
- Mock LLM berjalan di ``http://127.0.0.1:8099`` (jalankan ``python tests/mock_llm.py``)

Jalankan dengan:  python tests/e2e_gaps.py
"""

import asyncio
import json
import os
import sys

import httpx
import websockets

BASE = "http://127.0.0.1:8080/api/v1"
WS = "ws://127.0.0.1:8080/api/v1/ws"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "received_models.txt")
MOCK = "http://127.0.0.1:8099/v1"

CATALOG = {
    "version": 1,
    "services": {
        "llm": {
            "active_profile_id": "prof-1",
            "active_model_id": "mod-cepat",
            "profiles": [
                {
                    "id": "prof-1",
                    "name": "Mock Provider",
                    "binding": "vllm",
                    "base_url": MOCK,
                    "api_key": "",
                    "models": [
                        {"id": "mod-cepat", "name": "Cepat", "model": "model-cepat"},
                        {"id": "mod-lambat", "name": "Lambat", "model": "model-lambat"},
                    ],
                }
            ],
        },
        "embedding": {
            "active_profile_id": "emb-1",
            "active_model_id": "embmod-1",
            "profiles": [
                {
                    "id": "emb-1",
                    "name": "Embed",
                    "binding": "vllm",
                    "base_url": MOCK,
                    "api_key": "",
                    "models": [{"id": "embmod-1", "name": "bge", "model": "bge-m3"}],
                }
            ],
        },
    },
}

fail = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        fail.append(label)


async def collect(ws, until=("done",), timeout=60):
    events = []
    while True:
        event = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if event.get("type") in ("ping", "pong"):
            continue
        events.append(event)
        if event.get("type") in until:
            return events


async def main():
    if os.path.exists(LOG_PATH):
        os.remove(LOG_PATH)

    with httpx.Client(base_url=BASE, timeout=60.0) as client:
        response = client.post("/auth/login", json={"username": "admin", "password": "CHANGEME"})
        check("login admin", response.status_code == 200)
        if response.status_code != 200:
            return
        token = response.cookies.get("nalar_token")
        cookie = f"nalar_token={token}"
        client.post("/settings/apply", json={"catalog": CATALOG})

        # ── Celah 4: /system/status difilter per user ──
        status_authed = client.get("/system/status").json()
        check("status llm online (login)", status_authed["llm"]["status"] == "online", json.dumps(status_authed["llm"]))
        check("status embedding online", status_authed["embeddings"]["status"] == "online")

    with httpx.Client(base_url=BASE, timeout=60.0) as anon:
        status_anon = anon.get("/system/status").json()
        check(
            "status tanpa login tidak bocor",
            status_anon["llm"]["status"] == "not_configured",
            json.dumps(status_anon["llm"]),
        )

    # ── Celah 1: turn lanjut setelah koneksi terputus ──
    session_id = None
    turn_id = None
    last_seq = 0
    partial = ""
    async with websockets.connect(WS, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "message",
                    "content": "jawab panjang",
                    "llm_selection": {"profile_id": "prof-1", "model_id": "mod-lambat"},
                }
            )
        )
        # Ambil beberapa event awal saja, lalu putuskan di tengah streaming.
        for _ in range(3):
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            turn_id = event.get("turn_id") or turn_id
            last_seq = max(last_seq, event.get("seq") or 0)
            if event.get("type") == "session":
                session_id = (event.get("metadata") or {}).get("session_id")
            if event.get("type") == "content":
                partial += event.get("content") or ""
    check("dapat turn_id sebelum putus", bool(turn_id), str(turn_id))
    check("streaming sudah mulai", bool(partial), partial)

    # Koneksi baru: minta lanjutan dari seq terakhir yang diterima.
    async with websockets.connect(WS, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(json.dumps({"type": "resume_from", "turn_id": turn_id, "seq": last_seq}))
        events = await collect(ws)
    resumed = "".join(e.get("content") or "" for e in events if e.get("type") == "content")
    seqs = [e.get("seq") for e in events if e.get("seq")]
    check("resume mengirim lanjutan", bool(resumed), resumed)
    check("tidak ada event terkirim ulang", all(s > last_seq for s in seqs), f"last_seq={last_seq} seqs={seqs[:6]}")
    check("turn selesai completed", (events[-1].get("metadata") or {}).get("status") == "completed")
    check("jawaban utuh = sebagian + lanjutan", (partial + resumed).count("bagian") == 6, partial + resumed)

    with httpx.Client(base_url=BASE, timeout=60.0, cookies={"nalar_token": token}) as client:
        stored = client.get(f"/chat/sessions/{session_id}").json()["messages"]
        answer = next((m["content"] for m in stored if m["role"] == "assistant"), "")
        check("jawaban tersimpan utuh di DB", answer.count("bagian") == 6, answer)

    # ── Celah 2a: regenerate ──
    async with websockets.connect(WS, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(json.dumps({"type": "regenerate", "session_id": session_id}))
        events = await collect(ws)
    regenerated = "".join(e.get("content") or "" for e in events if e.get("type") == "content")
    check("regenerate menghasilkan jawaban", bool(regenerated), regenerated[:60])
    check("regenerate selesai completed", (events[-1].get("metadata") or {}).get("status") == "completed")

    with httpx.Client(base_url=BASE, timeout=60.0, cookies={"nalar_token": token}) as client:
        roles = [m["role"] for m in client.get(f"/chat/sessions/{session_id}").json()["messages"]]
        check("riwayat tetap 1 user + 1 assistant", roles == ["user", "assistant"], str(roles))

    # regenerate pada sesi tanpa pesan → nothing_to_regenerate
    async with websockets.connect(WS, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(json.dumps({"type": "regenerate", "session_id": "00000000-0000-0000-0000-000000000000"}))
        events = await collect(ws)
    reasons = [(e.get("metadata") or {}).get("reason") for e in events]
    check("regenerate sesi kosong ditolak", "nothing_to_regenerate" in reasons, str(reasons))

    # ── Celah 2b: submit_user_reply dijawab jelas, tidak menggantung ──
    async with websockets.connect(WS, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(json.dumps({"type": "submit_user_reply", "turn_id": turn_id, "text": "ya"}))
        events = await collect(ws)
    reasons = [(e.get("metadata") or {}).get("reason") for e in events]
    check("submit_user_reply ditolak jelas", "not_waiting_for_reply" in reasons, str(reasons))

    # ── cancel_turn benar-benar menghentikan ──
    async with websockets.connect(WS, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "message",
                    "content": "batalkan aku",
                    "llm_selection": {"profile_id": "prof-1", "model_id": "mod-lambat"},
                }
            )
        )
        cancel_turn_id = None
        for _ in range(2):
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            cancel_turn_id = event.get("turn_id") or cancel_turn_id
        await ws.send(json.dumps({"type": "cancel_turn", "turn_id": cancel_turn_id}))
        events = await collect(ws)
    check(
        "cancel_turn menutup turn sebagai cancelled",
        (events[-1].get("metadata") or {}).get("status") == "cancelled",
        json.dumps(events[-1].get("metadata")),
    )

    # ── subscribe ke turn milik orang lain / tidak ada → ditolak, tidak menggantung ──
    async with websockets.connect(WS, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(json.dumps({"type": "subscribe_turn", "turn_id": "tidak-ada", "after_seq": 0}))
        events = await collect(ws)
    reasons = [(e.get("metadata") or {}).get("reason") for e in events]
    check("subscribe turn tak dikenal ditolak", "turn_not_found" in reasons, str(reasons))

    print()
    print("GAGAL: " + (", ".join(fail) if fail else "tidak ada"))


asyncio.run(main())
sys.exit(1 if fail else 0)
