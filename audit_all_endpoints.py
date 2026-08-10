"""Audit seluruh permukaan API: login sekali, GET semua path tanpa parameter.

Gagal (exit 1) kalau ada 5xx atau 401/403 tak terduga. 404 pada path yang
memang belum diimplementasikan dilaporkan sebagai WARN, bukan FAIL.

ponytail: hanya GET tanpa path-param — POST/PUT/DELETE butuh body valid per
endpoint; naikkan ke kontrak per-endpoint kalau nanti perlu (verify_changes.py
sudah menangani endpoint settings secara mendalam).

Jalankan: venv/Scripts/python audit_all_endpoints.py
"""

import sys

import httpx

BASE = "http://127.0.0.1:8087"
USER, PASS = "debugger2", "debug1234"
EXPECTED_FORBIDDEN = {"/api/v1/auth/users"}


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=True) as c:
        spec_response = c.get("/openapi.json")
        if spec_response.status_code != 200:
            print(f"[FAIL] openapi {spec_response.status_code}: {spec_response.text[:200]}")
            return 1
        paths = spec_response.json()["paths"]

        r = c.post("/api/v1/auth/login", json={"username": USER, "password": PASS})
        if r.status_code != 200:
            print(f"[FAIL] login {r.status_code}: {r.text[:200]}")
            return 1
        print("[PASS] login")

        targets = []
        for path, operations in paths.items():
            operation = operations.get("get")
            if operation is None or "{" in path:
                continue
            required_query = any(
                parameter.get("required") and parameter.get("in") == "query"
                for parameter in operation.get("parameters", [])
            )
            if not required_query:
                targets.append(path)
        targets.sort()
        fails, warns, ok = [], [], 0

        for path in targets:
            try:
                resp = c.get(path)
            except Exception as exc:  # noqa: BLE001 - laporkan apa pun
                fails.append((path, "EXC", str(exc)[:120]))
                continue
            code = resp.status_code
            if code >= 500:
                fails.append((path, code, resp.text[:160]))
            elif code == 403 and path in EXPECTED_FORBIDDEN:
                warns.append((path, code))
            elif code in (401, 403):
                fails.append((path, code, "auth gagal padahal sudah login"))
            elif code == 404:
                warns.append((path, code))
            elif code >= 400:
                warns.append((path, code))
            else:
                ok += 1

    print(f"\n[OK] {ok}/{len(targets)} GET sehat (2xx/3xx)")
    for path, code in warns:
        print(f"[WARN] {code} {path}")
    for path, code, detail in fails:
        print(f"[FAIL] {code} {path} :: {detail}")

    print("\nRESULT:", "ADA MASALAH" if fails else "TIDAK ADA 5xx / AUTH BOCOR")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
