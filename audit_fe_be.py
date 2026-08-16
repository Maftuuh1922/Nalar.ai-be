"""Bandingkan endpoint yang dipanggil frontend dengan rute nyata backend.

Sumber kebenaran backend: app.main:app — skema OpenAPI ditambah rute WebSocket
(yang tidak muncul di OpenAPI). Sumber frontend: literal string "/api/v1/..."
di seluruh berkas .ts/.tsx.

Segmen dinamis (`${...}`, UUID, angka) dinormalkan jadi {X} di kedua sisi.
Frontend sering menulis nilai literal di posisi path-param backend (mis.
`/memory/doc/L2/x` vs `/memory/doc/{layer}/{doc_key}`), jadi pencocokan
dilakukan per-segmen dengan {X} berlaku sebagai wildcard.

Jalankan: venv/Scripts/python audit_fe_be.py [path_frontend]
"""

import json
import re
import sys
from pathlib import Path

PREFIX = "/api/v1"
URL_PATTERN = re.compile(r"""['"`](/api/v1/[^'"`\s]*)['"`]""")
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
# Literal yang bukan panggilan endpoint: konstanta BASE yang selalu disambung
# sufiks, contoh di komentar JSDoc, dan data fixture pada berkas uji.
IGNORED = {
    "/api/v1/book",  # const BASE di lib/book-api.ts
    "/api/v1/co_writer",  # const BASE/API di co-writer-api.ts & CoWriterChatPanel
    "/api/v1/solve",  # contoh path di docstring wsUrl(), lib/api.ts
}
IGNORED_DIRS = {"tests"}  # fixture & audit e2e, bukan panggilan runtime


def normalize(path: str) -> str:
    """Samakan bentuk path: buang query/hash, ganti segmen dinamis jadi {X}."""
    path = path.split("?")[0].split("#")[0].rstrip("/")
    segments = []
    for segment in path.split("/"):
        if not segment:
            segments.append(segment)
            continue
        dynamic = (
            "${" in segment
            or segment.startswith("{")
            or segment.isdigit()
            or UUID_PATTERN.match(segment)
        )
        segments.append("{X}" if dynamic else segment)
    return "/".join(segments)


def matches(call: str, route: str) -> bool:
    """Cocokkan path per-segmen; {X} di sisi mana pun berlaku sebagai wildcard."""
    call_parts, route_parts = call.split("/"), route.split("/")
    if len(call_parts) != len(route_parts):
        return False
    return all(
        c == r or c == "{X}" or r == "{X}"
        for c, r in zip(call_parts, route_parts)
    )


def collect_frontend(root: Path) -> dict[str, set[str]]:
    """Petakan path ternormalisasi -> berkas frontend yang memanggilnya."""
    callers: dict[str, set[str]] = {}
    skip = {"node_modules", ".next", ".git", "dist", "out"} | IGNORED_DIRS
    for file in root.rglob("*"):
        if file.suffix not in {".ts", ".tsx"} or not file.is_file():
            continue
        if any(part in skip for part in file.parts):
            continue
        try:
            source = file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for raw in URL_PATTERN.findall(source):
            if "..." in raw or "*" in raw:
                continue
            path = normalize(raw)
            if path and path != PREFIX and path not in IGNORED:
                callers.setdefault(path, set()).add(str(file.relative_to(root)))
    return callers


def main() -> int:
    frontend_root = Path(sys.argv[1] if len(sys.argv) > 1 else "../Nalar.ai_fe")
    if not frontend_root.is_dir():
        print(f"[FAIL] direktori frontend tidak ditemukan: {frontend_root}")
        return 1

    from fastapi.routing import APIWebSocketRoute
    from starlette.routing import WebSocketRoute

    from app.main import app

    spec = app.openapi()
    operations = sum(
        1
        for methods in spec["paths"].values()
        for method in methods
        if method in {"get", "post", "put", "patch", "delete"}
    )
    # Rute WebSocket tidak masuk skema OpenAPI, tapi tetap permukaan API nyata.
    # FastAPI membungkus router yang di-include sebagai `_IncludedRouter`, jadi
    # rutenya diambil lewat `original_router` beserta prefix include-nya.
    websockets = set()
    for entry in app.routes:
        original = getattr(entry, "original_router", None)
        if original is None:
            continue
        context = getattr(entry, "include_context", None)
        prefix = getattr(context, "prefix", "") if context else ""
        for route in original.routes:
            if isinstance(route, (APIWebSocketRoute, WebSocketRoute)):
                websockets.add(normalize(prefix + route.path))
    backend = {normalize(p) for p in spec["paths"]} | websockets

    frontend = collect_frontend(frontend_root)
    missing = {
        path: files
        for path, files in frontend.items()
        if not any(matches(path, route) for route in backend)
    }

    print(f"Backend : {len(spec['paths'])} path / {operations} operasi "
          f"(+{len(websockets)} websocket)")
    print(f"Frontend: {len(frontend)} path unik dipanggil")
    print(f"Cocok   : {len(frontend) - len(missing)}")
    print(f"Hilang  : {len(missing)}\n")

    for path in sorted(missing):
        files = ", ".join(sorted(missing[path])[:3])
        print(f"[MISSING] {path}\n           dipanggil di: {files}")

    json.dump(
        {"missing": {p: sorted(f) for p, f in missing.items()}},
        open("_audit_result.json", "w", encoding="utf-8"),
        indent=1,
        ensure_ascii=False,
    )
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
