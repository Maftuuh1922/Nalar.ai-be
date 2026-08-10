"""Mock LLM server untuk uji E2E WebSocket ChatOrchestrator.

Dipakai untuk uji celah: reconnect/resume, regenerate, cancel, status per user,
dan normalisasi.  Server ini mendukung:

- ``POST /v1/chat/completions`` — streaming (SSE-like NDJSON) dan non-streaming
- ``POST /v1/embeddings`` — endpoint embedding sederhana

Balasan selalu memuat kata "bagian" tepat 6 kali (6 chunk) sehingga test bisa
memastikan jawaban utuh setelah reconnect.  Model dengan nama mengandung
"lambat" distream lambat (1.2 s per chunk) supaya koneksi bisa diputus di
tengah streaming.

Jalankan sebelum menjalankan test:

    python tests/mock_llm.py            # :8099
"""

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "received_models.txt")

CHUNKS = [
    "Ini bagian satu. ",
    "Lalu bagian dua. ",
    "Kemudian bagian tiga. ",
    "Berikut bagian empat. ",
    "Menyusul bagian lima. ",
    "Terakhir bagian enam.",
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path.endswith("/chat/completions"):
            self._chat()
        elif self.path.endswith("/embeddings"):
            self._embeddings()
        else:
            self.send_error(404)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _chat(self):
        body = self._read_body()
        model = body.get("model", "unknown")
        stream = bool(body.get("stream"))
        delay = 1.2 if "lambat" in str(model) else 0.02

        try:
            with open(LOG_PATH, "a", encoding="utf-8") as handle:
                handle.write(f"{model}\n")
        except OSError:
            pass

        if not stream:
            content = "".join(CHUNKS)
            payload = json.dumps(
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 24, "total_tokens": 32},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self._cors()
        self.end_headers()

        resp_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        def send_sse(obj) -> bool:
            raw = f"data: {json.dumps(obj)}\n\n".encode()
            try:
                self.wfile.write(f"{len(raw):X}\r\n".encode() + raw + b"\r\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        for chunk in CHUNKS:
            if not send_sse(
                {
                    "id": resp_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                }
            ):
                return
            time.sleep(delay)

        send_sse(
            {
                "id": resp_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        try:
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _embeddings(self):
        body = self._read_body()
        raw_input = body.get("input") or []
        items = raw_input if isinstance(raw_input, list) else [raw_input]
        payload = json.dumps(
            {
                "object": "list",
                "model": body.get("model", "bge-m3"),
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.01] * 8}
                    for i, _ in enumerate(items)
                ],
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8099), Handler).serve_forever()
