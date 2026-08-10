"""Model penalaran yang menghabiskan anggaran token sebelum menjawab.

Cacat yang diuji di sini terukur pada model aktif proyek ini
(``moonshotai/kimi-k3-free``): dengan ``max_tokens=800`` aliran berakhir
``finish_reason="length"`` setelah ~2.900 aksara jejak penalaran dan **nol**
aksara jawaban, sehingga chat dokumen selalu menampilkan pesan cadangan "belum
memperoleh jawaban". Daftar kemampuan di pengaturan tidak bisa dipakai sebagai
penanda karena model itu mendaftarkan diri sebagai ``["text", "tools"]``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.agentic_writer import AgenticWriter
from app.services.model_selection import ResolvedLLM


@dataclass
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None


@dataclass
class _Pilihan:
    delta: _Delta
    finish_reason: str | None = None


@dataclass
class _Chunk:
    choices: list[_Pilihan]


class _AliranPalsu:
    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _CompletionsPalsu:
    """Meniru gateway: jejak penalaran datang sebagai delta terpisah."""

    def __init__(self, panjang_penalaran: int, ambang_jawaban: int) -> None:
        self.panggilan: list[int] = []
        self._panjang_penalaran = panjang_penalaran
        self._ambang_jawaban = ambang_jawaban

    async def create(self, **request):
        max_tokens = request["max_tokens"]
        self.panggilan.append(max_tokens)
        chunks = [
            _Chunk([_Pilihan(_Delta(reasoning_content="p" * self._panjang_penalaran))])
        ]
        if max_tokens >= self._ambang_jawaban:
            chunks.append(_Chunk([_Pilihan(_Delta(content="Jawaban nyata."), "stop")]))
        else:
            chunks.append(_Chunk([_Pilihan(_Delta(), "length")]))
        return _AliranPalsu(chunks)


def _writer(completions: _CompletionsPalsu) -> AgenticWriter:
    llm = ResolvedLLM(
        model_name="uji/model-penalaran",
        base_url="http://localhost:9",
        api_key="dummy",
        provider_type="openai",
        context_window=65_536,
        capability_tier="standard",
        capabilities=["text", "tools"],
    )
    writer = AgenticWriter(llm)

    class _Chat:
        completions = None

    chat = _Chat()
    chat.completions = completions
    writer._client.chat = chat  # noqa: SLF001 — menyuntik ganda tanpa jaringan
    return writer


@pytest.mark.anyio
async def test_anggaran_habis_untuk_penalaran_diulang_dengan_anggaran_lebih_besar():
    completions = _CompletionsPalsu(panjang_penalaran=2900, ambang_jawaban=2500)
    writer = _writer(completions)

    hasil = await writer.ask("pertanyaan", system="sistem", max_tokens=800)

    assert hasil == "Jawaban nyata."
    assert completions.panggilan == [800, 2500], "harus mengulang sekali dengan anggaran naik"


@pytest.mark.anyio
async def test_jawaban_pada_percobaan_pertama_tidak_memicu_panggilan_kedua():
    completions = _CompletionsPalsu(panjang_penalaran=50, ambang_jawaban=0)
    writer = _writer(completions)

    hasil = await writer.ask("pertanyaan", system="sistem", max_tokens=800)

    assert hasil == "Jawaban nyata."
    assert completions.panggilan == [800], "model yang sudah menjawab tak boleh dipanggil ulang"


@pytest.mark.anyio
async def test_kosong_tanpa_jejak_penalaran_tidak_diulang():
    """Batas token yang habis untuk JAWABAN adalah kasus lain: mengulanginya
    hanya membakar kuota tanpa mengubah hasil."""
    completions = _CompletionsPalsu(panjang_penalaran=0, ambang_jawaban=10**9)
    writer = _writer(completions)

    hasil = await writer.ask("pertanyaan", system="sistem", max_tokens=800)

    assert hasil == ""
    assert completions.panggilan == [800]
