"""Uji ujung-ke-ujung: apakah agen benar-benar memakai `cite_add` dan menulis `[n]`.

Butuh proxy LLM lokal hidup di http://localhost:20128/v1 (model `ai`).
Jalankan dari direktori Nalar.ai-be:

    ./.venv/bin/python uji_agen_sitasi.py

Skrip ini TIDAK menulis ke dokumen mana pun: tool tulis (`fe: true`) dieksekusi
frontend, jadi di sini ia hanya dicatat. Sumber yang tersimpan agen dibersihkan
kembali di akhir supaya perpustakaan referensimu tidak terisi data uji.
"""

import asyncio
import json
import re

from openai import AsyncOpenAI
from sqlalchemy import delete, select

from app.db.session import AsyncSessionLocal
from app.models.journal import JournalGroup, JournalReference
from app.models.model_config import ModelConfig
from app.services.agent_run import run_agent_stream
from app.services.citation_formatter import (
    citation_meta_from_reference,
    generate_citation,
)
from app.services.citation_tools import NAMA_GRUP_AGEN, referensi_urut
from app.services.model_selection import resolve_llm

DOK = (
    "# Bab 2 Tinjauan Pustaka\n\n"
    "## 2.1 Kajian Pustaka\n\n"
    "Bagian ini membahas dasar teori yang dipakai penelitian.\n"
)

INSTRUKSI = (
    "Tambahkan sub-bab 2.2 tentang Large Language Model dan arsitektur "
    "Transformer, lalu 2.3 tentang Retrieval-Augmented Generation. Setiap klaim "
    "penting harus merujuk sumber nyata."
)


async def bersihkan(db, user_id) -> int:
    """Hapus sumber hasil uji (hanya grup agen)."""
    n = 0
    for g in await db.scalars(
        select(JournalGroup).where(
            JournalGroup.user_id == user_id, JournalGroup.name == NAMA_GRUP_AGEN
        )
    ):
        n += len(list(await db.scalars(
            select(JournalReference).where(JournalReference.group_id == g.id)
        )))
        await db.execute(delete(JournalReference).where(JournalReference.group_id == g.id))
        await db.execute(delete(JournalGroup).where(JournalGroup.id == g.id))
    await db.commit()
    return n


async def main() -> None:
    async with AsyncSessionLocal() as db:
        cfg = [x for x in await db.scalars(select(ModelConfig)) if x.is_active]
        if not cfg:
            print("Tidak ada konfigurasi model aktif.")
            return
        llm = await resolve_llm(db, cfg[0].user_id)
        user_id = cfg[0].user_id
        print(f"model={llm.model_name}  base_url={llm.base_url}  tier={llm.capability_tier}")

        client = AsyncOpenAI(base_url=llm.base_url, api_key=llm.api_key, timeout=180)
        try:
            await client.chat.completions.create(
                model=llm.model_name,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=8,
            )
        except Exception as exc:  # noqa: BLE001
            pesan = str(exc)
            if "429" in pesan or "FreeUsage" in pesan:
                print("Gateway membatasi kuota (429). Coba lagi nanti.")
            else:
                print(f"Proxy tidak bisa dihubungi: {pesan[:160]}")
            return
        print("kuota gateway: OK\n")

        await bersihkan(db, user_id)
        ditulis: list[str] = []
        dipakai_cite_add = 0
        event: dict[str, int] = {}

        async for line in run_agent_stream(
            client,
            llm.model_name,
            instruction=INSTRUKSI,
            doc_context=DOK,
            db=db,
            user_id=user_id,
            mode="seimbang",
            allow_web=True,
            context_window=llm.context_window,
        ):
            ev = json.loads(line)
            nama = ev["event"]
            event[nama] = event.get(nama, 0) + 1
            data = ev.get("data")
            if nama == "plan":
                print("RENCANA:", [t["title"][:50] for t in data["tasks"]])
            elif nama == "task_status":
                print(f"  status tugas #{data.get('index')}: {data.get('status')}")
            elif nama == "tool_call":
                if data["name"] == "cite_add":
                    dipakai_cite_add += 1
                if data["name"] == "doc_insert":
                    ditulis.append(str(data["args"].get("markdown", "")))
                print(f"  TOOL {data['name']}: {str(data.get('args'))[:96]}")
            elif nama == "tool_result":
                print(f"       -> ok={data['ok']} {str(data.get('summary'))[:70]}")
            elif nama == "error":
                print("  ERROR:", str(data)[:180])
            elif nama == "text":
                print("\nRINGKASAN:", str(data)[:400])

        daftar = await referensi_urut(db, user_id)
        nomor = sorted({int(n) for t in ditulis for n in re.findall(r"\[(\d+)\]", t)})

        print("\n" + "=" * 60)
        print("event                :", event)
        print("panggilan cite_add   :", dipakai_cite_add)
        print("sumber tersimpan     :", len(daftar))
        print("nomor [n] di naskah  :", nomor or "(tidak ada)")
        if nomor:
            sah = all(1 <= n <= len(daftar) for n in nomor)
            print("semua nomor sah?     :", sah)
            if not sah:
                print("  !! ada nomor di luar rentang perpustakaan — agen mengarang")
        if daftar:
            print("\nDaftar Pustaka yang akan tercetak:")
            for i, r in enumerate(daftar, 1):
                dipakai = "dipakai" if i in nomor else "TIDAK dipakai di naskah"
                entri = generate_citation(citation_meta_from_reference(r), "ieee")
                print(f"  [{i}] {entri[:88]}  ({dipakai})")

        n = await bersihkan(db, user_id)
        print(f"\n{n} sumber uji dibersihkan dari perpustakaan.")


if __name__ == "__main__":
    asyncio.run(main())
