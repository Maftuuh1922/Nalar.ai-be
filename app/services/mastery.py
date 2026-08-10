"""Algoritma Mastery Scoring disalin & disesuaikan dari Nalar AI (nalar-ai/learning/mastery.py).

Fungsi ``compute_mastery`` mengubah riwayat hasil percobaan kuis menjadi skor penguasaan 0.0 .. 1.0.
Algoritma ini menggunakan pembobotan berbasis kebaruan (recency-weighted accuracy)
serta batas kepercayaan rendah (low-confidence cap) agar 1-2 hasil kebetulan tidak langsung menandai topik "dikuasai".
"""

from __future__ import annotations

# Bobot kebaruan dari percobaan paling lama -> paling baru dalam rentang 5 percobaan terakhir.
_RECENCY_WEIGHTS: tuple[float, ...] = (0.5, 0.7, 0.85, 0.95, 1.0)

# Batas skor penguasaan maksimum jika data percobaan masih sangat sedikit
_CONFIDENCE_CAP: dict[int, float] = {1: 0.5, 2: 0.8}


def compute_mastery(correctness: list[bool]) -> float:
    """Menghitung skor penguasaan 0.0 .. 1.0 berdasarkan daftar kebenaran jawaban kuis secara kronologis.

    Args:
        correctness: List boolean urut dari yang paling lama ke yang terbaru.
                     True = benar, False = salah.
    """
    if not correctness:
        return 0.0

    # Ambil hingga 5 percobaan terbaru
    recent = correctness[-len(_RECENCY_WEIGHTS) :]
    weights = _RECENCY_WEIGHTS[-len(recent) :]

    score = sum(w * (1.0 if c else 0.0) for c, w in zip(recent, weights, strict=True)) / sum(weights)
    
    # Terapkan confidence cap jika percobaan < 3
    capped_score = min(score, _CONFIDENCE_CAP.get(len(recent), 1.0))
    return round(capped_score, 4)


def mastery_level_label(mastery_score: float) -> str:
    """Mengembalikan label status penguasaan materi."""
    if mastery_score >= 0.85:
        return "Sangat Menguasai"
    elif mastery_score >= 0.70:
        return "Menguasai"
    elif mastery_score >= 0.50:
        return "Berkembang"
    elif mastery_score > 0.0:
        return "Perlu Latihan"
    return "Belum Ada Data"


__all__ = ["compute_mastery", "mastery_level_label"]
