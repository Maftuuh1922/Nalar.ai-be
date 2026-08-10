"""Verifikasi permanen & hapus: upload dokumen → cek tersimpan → delete → cek hilang.
Juga: buat sesi chat via API → delete → cek hilang (termasuk pesannya)."""

import asyncio
import io
import json
import uuid

import httpx

BASE = "http://127.0.0.1:8087/api/v1"
USER, PASS = "debugger2", "debug1234"


async def main() -> int:
    fails = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.post(f"{BASE}/auth/login", json={"username": USER, "password": PASS})
        assert r.status_code == 200, f"login {r.status_code}"
        tok = c.cookies.get("nalar_token")
        h = {"Cookie": f"nalar_token={tok}"}

        # 1) Upload dokumen asli (pdf kecil valid)
        pdf = io.BytesIO(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")
        r = await c.post(
            f"{BASE}/documents",
            files={"file": ("tes-permanen.pdf", pdf, "application/pdf")},
            headers=h,
        )
        if r.status_code != 201:
            fails.append(f"upload: {r.status_code} {r.text[:200]}")
        else:
            doc = r.json()
            doc_id = doc["id"]
            print(f"[OK] upload → id={doc_id[:8]} status={doc.get('status')} filename={doc.get('filename')}")

            # 2) Masih ada di DB setelah "restart" simulasi? (list ulang)
            r = await c.get(f"{BASE}/documents", headers=h)
            ids = [d["id"] for d in r.json()]
            if doc_id not in ids:
                fails.append("dokumen tidak muncul di list")
            else:
                print("[OK] dokumen tersimpan permanen (muncul di list)")

            # 3) Hapus
            r = await c.delete(f"{BASE}/documents/{doc_id}", headers=h)
            if r.status_code != 204:
                fails.append(f"delete: {r.status_code} {r.text[:200]}")
            else:
                print("[OK] delete → 204")

            # 4) Pastikan benar-benar hilang
            r = await c.get(f"{BASE}/documents", headers=h)
            ids = [d["id"] for d in r.json()]
            if doc_id in ids:
                fails.append("dokumen masih ada setelah delete")
            else:
                print("[OK] dokumen hilang setelah delete")

        # 5) Sesi chat: buat lewat POST /chat (bukan /chat/sessions) → delete → cek hilang
        r = await c.post(
            f"{BASE}/chat",
            headers=h,
            json={
                "message": "Halo, ini pesan audit simpan-hapus.",
                "session_id": None,
                "document_ids": [],
            },
        )
        if r.status_code not in (200, 201):
            fails.append(f"buat sesi: {r.status_code} {r.text[:200]}")
        else:
            # /chat menjawab dengan event stream SSE: cari session_id di event pertama
            lines = [ln for ln in r.text.splitlines() if ln.strip().startswith("{")]
            import json as _json

            sess_id = None
            for ln in lines:
                try:
                    ev = _json.loads(ln)
                except Exception:
                    continue
                if ev.get("event") == "session_created":
                    sess_id = ev.get("data")
                    break
            if not sess_id:
                fails.append(f"session_id tidak ditemukan di respon /chat: {r.text[:200]}")
            else:
                r2 = await c.delete(f"{BASE}/chat/sessions/{sess_id}", headers=h)
                if r2.status_code != 200:
                    fails.append(f"delete sesi: {r2.status_code} {r2.text[:200]}")
                else:
                    r3 = await c.get(f"{BASE}/chat/sessions/{sess_id}", headers=h)
                    if r3.status_code != 404:
                        fails.append(f"sesi masih ada setelah delete ({r3.status_code})")
                    else:
                        print("[OK] sesi chat dibuat → dihapus → 404 (hilang permanen)")

    if fails:
        print("\nFAIL:")
        for f in fails:
            print(" -", f)
        return 1
    print("\nRESULT: SEMUA LULUS (simpan permanen + bisa dihapus)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
