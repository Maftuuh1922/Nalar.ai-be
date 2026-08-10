"""Audit halaman: render setiap rute Next.js dengan cookie login nyata.

Gagal (exit 1) kalau ada rute yang mengembalikan 5xx, redirect ke /login,
atau HTML-nya memuat penanda error Next.js (error overlay / digest).

ponytail: hanya memeriksa HTML hasil SSR — interaksi klien (klik, fetch dari
browser) tidak diuji di sini. Kalau nanti butuh, jalankan Playwright.

Prasyarat: frontend production di :3000 (npm run start) dan backend di :8087.
Jalankan: venv/Scripts/python audit_pages.py
"""

import re
import sys
from pathlib import Path

import httpx

FE = "http://127.0.0.1:3000"
BE = "http://127.0.0.1:8087"
USER, PASS = "debugger2", "debug1234"

FRONTEND_APP = Path(__file__).resolve().parent.parent / "Nalar.ai_fe" / "app"


def discover_routes() -> list[str]:
    """Temukan rute statis App Router; route dinamis diuji oleh audit workflow."""
    routes: set[str] = set()
    for page in FRONTEND_APP.rglob("page.tsx"):
        parts = []
        dynamic = False
        for part in page.relative_to(FRONTEND_APP).parts[:-1]:
            if part.startswith("(") and part.endswith(")"):
                continue
            if "[" in part or "]" in part:
                dynamic = True
                break
            parts.append(part)
        if not dynamic:
            routes.add("/" + "/".join(parts) if parts else "/")
    return sorted(routes)

# Penanda error runtime Next.js/React di HTML hasil render.
ERROR_MARKERS = (
    "Application error: a client-side exception",
    "Internal Server Error",
    "__next_error__",
    "nextjs-portal",
)


def main() -> int:
    routes = discover_routes()
    with httpx.Client(timeout=60.0, follow_redirects=False) as c:
        r = c.post(f"{BE}/api/v1/auth/login", json={"username": USER, "password": PASS})
        if r.status_code != 200:
            print(f"[FAIL] login backend {r.status_code}")
            return 1
        token = r.cookies.get("nalar_token")
        if not token:
            print("[FAIL] cookie nalar_token tidak diterima")
            return 1
        print("[PASS] login, cookie diperoleh")

        fails, warns, ok = [], [], 0
        for route in routes:
            try:
                resp = c.get(f"{FE}{route}", cookies={"nalar_token": token})
            except Exception as exc:  # noqa: BLE001
                fails.append((route, "EXC", str(exc)[:120]))
                continue
            code = resp.status_code
            body = resp.text if code == 200 else ""
            if code >= 500:
                fails.append((route, code, body[:160]))
            elif code in (307, 302, 301):
                loc = resp.headers.get("location", "")
                if "/login" in loc and route not in ("/login", "/register"):
                    fails.append((route, code, f"redirect ke login: {loc}"))
                else:
                    warns.append((route, code, loc))
            elif code == 404:
                fails.append((route, code, "rute tidak ada"))
            elif code == 200:
                hit = next((m for m in ERROR_MARKERS if m in body), None)
                if hit:
                    fails.append((route, 200, f"penanda error: {hit}"))
                elif not re.search(r"<body[^>]*>", body):
                    fails.append((route, 200, "HTML tanpa <body>"))
                else:
                    ok += 1
            else:
                warns.append((route, code, ""))

    print(f"\n[OK] {ok}/{len(routes)} halaman render bersih")
    for route, code, detail in warns:
        print(f"[WARN] {code} {route} {detail}")
    for route, code, detail in fails:
        print(f"[FAIL] {code} {route} :: {detail}")
    print("\nRESULT:", "ADA MASALAH" if fails else "SEMUA HALAMAN SEHAT")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
