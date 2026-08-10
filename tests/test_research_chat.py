import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.agentic_writer import AgenticWriter
from app.services.research_chat import (
    build_research_prompt,
    classify_research_mode,
    format_history,
    input_char_budget,
    output_token_budget,
    select_document_evidence,
    select_reference_evidence,
    should_use_web,
    validate_citations,
)


def _reference(index: int, title: str, abstract: str = ""):
    return SimpleNamespace(
        id=str(index),
        title=title,
        filename=f"ref-{index}.pdf",
        authors=[f"Penulis {index}"],
        year=2020 + index,
        journal_name="Jurnal Uji",
        doi=f"10.1000/{index}",
        abstract=abstract,
        created_at=datetime(2020, 1, 1) + timedelta(days=index),
    )


def test_memilih_bab_relevan_bukan_hanya_awal_dokumen():
    files = {
        "main.tex": "\\input{bab/01}\n\\input{bab/04}",
        "bab/01.tex": "\\section{Pendahuluan}\nLatar belakang umum.",
        "bab/04.tex": (
            "\\section{Pengujian}\n"
            "Evaluasi menggunakan precision recall dan confusion matrix."
        ),
    }

    evidence = select_document_evidence("jelaskan confusion matrix", files)

    assert evidence.sections[0] == "bab/04.tex: Pengujian"
    assert "confusion matrix" in evidence.context


def test_referensi_relevan_mempertahankan_nomor_global():
    refs = [
        _reference(1, "Pembelajaran adaptif"),
        _reference(2, "Analisis confusion matrix", "precision dan recall"),
        _reference(3, "Desain antarmuka"),
    ]

    evidence = select_reference_evidence("precision recall", refs, max_references=1)

    assert evidence.numbers == [2]
    assert evidence.context.startswith("[2]")


def test_nomor_sitasi_rekaan_dihapus_tetapi_yang_valid_dipertahankan():
    reply, invalid = validate_citations(
        "Temuan didukung [2, 99], sedangkan klaim lain memakai [7].",
        {2, 3},
    )

    assert "[2]" in reply
    assert "[99]" not in reply
    assert "[7]" not in reply
    assert invalid == [7, 99]


def test_riwayat_dibatasi_dan_giliran_terbaru_dipertahankan():
    history = [
        {"role": "user", "content": "lama " * 500},
        {"role": "assistant", "content": "jawaban lama " * 500},
        {"role": "user", "content": "pertanyaan terbaru"},
    ]

    formatted = format_history(history, max_chars=500)

    assert len(formatted) <= 500
    assert "pertanyaan terbaru" in formatted


def test_mode_dan_web_ditentukan_tanpa_llm_tambahan():
    assert classify_research_mode("kritik kelemahan metodologi saya") == "critique"
    assert classify_research_mode("susun tinjauan pustaka") == "literature"
    assert should_use_web("cari penelitian terbaru tahun 2026") is True
    assert should_use_web("jelaskan isi bab metode") is False


def test_prompt_membedakan_sumber_dan_instruksi_pengguna():
    prompt = build_research_prompt(
        message="Apa kelemahan metode saya?",
        mode="critique",
        document_context="SUMBER DOKUMEN | bab/03.tex | Metode\nIsi metode",
        reference_context="[4] Referensi metode",
        history_context="PENGGUNA: bahas bab tiga",
    )

    assert "REFERENSI TERPILIH" in prompt
    assert "[4] Referensi metode" in prompt
    assert "MODE TUGAS: critique" in prompt


def test_prompt_ringkas_memaksa_jawaban_selesai_dalam_anggaran_kecil():
    prompt = build_research_prompt(
        message="Jawab ringkas tentang metode saya",
        mode="methodology",
        document_context="Isi metode",
        reference_context="(belum ada referensi)",
        history_context="(belum ada riwayat)",
    )

    assert "Maksimal 220 kata" in prompt
    assert "Tuntaskan jawaban" in prompt


def test_anggaran_output_adaptif_tanpa_menambah_panggilan_llm():
    assert output_token_budget("jawab singkat", "drafting", 16_000) == 600
    assert output_token_budget("susun bab lengkap", "drafting", 16_000) == 1600
    assert output_token_budget("kritik metodologi", "critique", 16_000) == 1000
    assert output_token_budget("jawab pertanyaan", "question", 3_000) == 500


def test_model_penalaran_dapat_cadangan_token_di_atas_anggaran_jawaban():
    """max_tokens mencakup token penalaran yang tak pernah sampai ke pengguna.

    Tanpa cadangan, anggaran 800 token habis untuk berpikir dan chat dokumen
    mengembalikan balasan kosong — pengguna melihat pesan "belum memperoleh
    jawaban" padahal modelnya sehat.
    """
    biasa = output_token_budget("jawab pertanyaan", "question", 65_536)
    dengan_penalaran = output_token_budget(
        "jawab pertanyaan", "question", 65_536, reasoning=True
    )

    assert biasa == 800
    assert dengan_penalaran == 2_000
    # Jendela konteks sempit tetap membatasi: cadangan tidak boleh menembus cap.
    assert output_token_budget("jawab pertanyaan", "question", 3_000, reasoning=True) == 500


def test_anggaran_input_memangkas_konteks_untuk_model_murah():
    assert input_char_budget("jawab ringkas", "critique", 1_000_000) == 8_000
    assert input_char_budget("kritik metodologi", "critique", 1_000_000) == 11_000
    assert input_char_budget("susun bab lengkap", "drafting", 1_000_000) == 20_000
    assert input_char_budget("jawab pertanyaan", "question", 3_000) == 7_000


def test_agentic_writer_mengirim_reasoning_effort_hanya_bila_diminta():
    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    create = AsyncMock(return_value=EmptyStream())
    writer = AgenticWriter(
        SimpleNamespace(
            base_url="http://127.0.0.1:9999/v1",
            api_key="dummy",
            model_name="reasoning-model",
        )
    )
    writer._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    asyncio.run(
        writer.ask(
            "uji",
            system="sistem",
            max_tokens=600,
            reasoning_effort="low",
        )
    )

    assert create.await_args.kwargs["reasoning_effort"] == "low"
    assert create.await_args.kwargs["max_tokens"] == 600
