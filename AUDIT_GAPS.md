# Audit celah backend vs frontend — Nalar.ai

**Status: TIDAK ADA CELAH.** Seluruh endpoint yang dipanggil frontend sudah
tersedia di backend.

| | |
|---|---|
| Backend | 231 path / 300 operasi + 1 WebSocket (`/api/v1/ws`) |
| Frontend | 142 path unik dipanggil |
| Cocok | 142 |
| Hilang | **0** |

Audit dijalankan ulang dari sumber kebenaran yang hidup — skema OpenAPI
`app.main:app` dibanding literal `/api/v1/...` di berkas `.ts`/`.tsx`:

```bash
venv/Scripts/python audit_fe_be.py
```

Skrip keluar dengan kode 1 kalau ada celah, jadi bisa dipasang di CI.

---

## Cakupan per router

| Router | Path | Operasi |
|---|---:|---:|
| co_writer | 43 | 53 |
| settings | 35 | 47 |
| knowledge | 27 | 32 |
| memory | 20 | 22 |
| journal | 13 | 21 |
| auth | 12 | 13 |
| questions | 11 | 18 |
| chat | 8 | 10 |
| skills | 8 | 12 |
| subagents | 8 | 8 |
| learning | 7 | 7 |
| notebook | 5 | 7 |
| quizzes | 4 | 5 |
| research | 4 | 6 |
| documents | 3 | 4 |
| notebooks | 3 | 6 |
| plugins | 3 | 3 |
| agents | 2 | 5 |
| multi-user | 2 | 2 |
| voice | 2 | 4 |
| book · capabilities · dashboard · imports · preferences · progress · question · system · tools · visualize · health | 1 tiap | 1–2 tiap |

---

## Catatan pencocokan

Empat pola di bawah sempat terbaca sebagai celah pada audit versi lama.
Semuanya **bukan** celah — skrip sekarang menanganinya:

1. **Literal vs path-param.** Frontend menulis nilai konkret di posisi
   parameter backend — `/memory/doc/L2/{id}` melawan
   `/memory/doc/{layer}/{doc_key}`, `/knowledge/rag-pipelines/llamaindex/config`
   melawan `/rag-pipelines/{provider}/config`. Pencocokan dilakukan per-segmen
   dengan wildcard di kedua sisi.
2. **WebSocket tidak ada di OpenAPI.** `/api/v1/ws` ([ws_chat.py:862](app/api/routes/ws_chat.py))
   nyata terdaftar tapi absen dari skema. FastAPI juga membungkus router
   ter-*include* sebagai `_IncludedRouter`, jadi rutenya diambil lewat
   `original_router`.
3. **Konstanta `BASE`.** `/api/v1/book` dan `/api/v1/co_writer` adalah prefix
   yang selalu disambung sufiks, bukan endpoint mandiri.
4. **Berkas uji & docstring.** `/api/v1/files/frame-1.png` (fixture) dan
   `/api/v1/solve` (contoh di JSDoc `wsUrl()`) tidak pernah dipanggil.

---

## Riwayat

Versi sebelumnya melaporkan *"87 dari 129 path tidak ada di backend"* dan
menyebut router memory, knowledge, question-notebook, skills, learning,
plugins, book, voice, subagents, imports, dan auth profile **belum ada sama
sekali**. Laporan itu sudah kedaluwarsa: seluruh router tersebut kini
terdaftar di [main.py:118-160](app/main.py). Angka lama juga membesar karena
keempat pola di atas terhitung sebagai celah.
