"""Diagnosa endpoint AI: uji koneksi nyata dan deteksi kemampuan model.

Berbeda dengan tebakan berdasarkan nama model, modul ini benar-benar memanggil
endpoint milik user — daftar model, satu chat completion kecil, satu percobaan
tool-calling, satu gambar 1x1 piksel, dan satu embedding. Setiap langkah punya
batas waktu sendiri supaya endpoint yang lambat tidak menggantung permintaan.
"""

import asyncio
import logging
import time
from typing import Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Batas waktu per langkah pemeriksaan (detik). Endpoint lokal seperti Ollama
# bisa lambat saat model belum dimuat, jadi jangan terlalu ketat.
PROBE_TIMEOUT = 25.0

# PNG 1x1 piksel transparan — muatan gambar terkecil yang masih sah.
_TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

# Jendela konteks umum berdasarkan potongan nama model. Dipakai sebagai
# perkiraan awal; user tetap bisa menimpanya lewat form pengaturan.
_CONTEXT_HINTS: tuple[tuple[str, int], ...] = (
    ("gemini-1.5", 1_000_000),
    ("gemini-2", 1_000_000),
    ("gemini", 128_000),
    ("claude", 200_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_192),
    ("gpt-3.5", 16_385),
    ("o1", 200_000),
    ("o3", 200_000),
    ("deepseek", 65_536),
    ("qwen", 32_768),
    ("llama-3", 128_000),
    ("mistral", 32_768),
    ("mixtral", 32_768),
)


def guess_provider_type(base_url: str) -> str:
    """Tebak jenis penyedia dari base URL."""
    url = (base_url or "").lower()
    if "generativelanguage.googleapis" in url or "/google" in url:
        return "google"
    if "anthropic" in url:
        return "anthropic"
    if "11434" in url or "ollama" in url:
        return "ollama"
    return "openai-compatible"


def guess_context_window(model_name: str) -> int:
    """Perkirakan jendela konteks dari nama model."""
    name = (model_name or "").lower()
    for needle, size in _CONTEXT_HINTS:
        if needle in name:
            return size
    return 65_536


def capabilities_from_name(model_name: str) -> set[str]:
    """Tebakan awal kemampuan berdasarkan nama model.

    Dipakai sebagai pelengkap, bukan pengganti, hasil pemeriksaan nyata —
    beberapa endpoint menolak permintaan uji tapi tetap mendukung fiturnya.
    """
    name = (model_name or "").lower()
    caps = {"text"}
    if any(x in name for x in ("vision", "-vl", "vl-", "multimodal", "gpt-4o", "gpt-4.1", "gemini", "claude-3", "claude-4", "llava", "pixtral", "omni")):
        caps.add("vision")
    if any(x in name for x in ("code", "coder", "codestral", "starcoder")):
        caps.add("code")
    if any(x in name for x in ("tts", "audio", "speech", "whisper", "voice")):
        caps.add("audio")
    if any(x in name for x in ("o1", "o3", "r1", "reason", "think", "qwq")):
        caps.add("reasoning")
    return caps


async def _timed(coro: Any) -> tuple[Any, int]:
    """Jalankan coroutine dengan batas waktu dan kembalikan lamanya (ms)."""
    start = time.perf_counter()
    result = await asyncio.wait_for(coro, timeout=PROBE_TIMEOUT)
    return result, int((time.perf_counter() - start) * 1000)


def _short(exc: Exception, limit: int = 160) -> str:
    """Ringkas pesan error agar enak dibaca di antarmuka."""
    text = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    return text[:limit] + ("..." if len(text) > limit else "")


async def probe_endpoint(
    base_url: str,
    api_key: str,
    model_name: str = "",
    embedding_model: str = "",
) -> dict[str, Any]:
    """Periksa endpoint AI dan simpulkan kemampuannya.

    Mengembalikan dict siap dipetakan ke ``DetectResponse``.
    """
    client = AsyncOpenAI(api_key=api_key or "dummy", base_url=base_url, max_retries=0)
    probes: list[dict[str, Any]] = []
    caps: set[str] = capabilities_from_name(model_name)
    available: list[str] = []
    reachable = False

    # 1. Daftar model — sekaligus membuktikan URL dan API key benar.
    try:
        listing, ms = await _timed(client.models.list())
        available = sorted({getattr(m, "id", "") for m in listing.data if getattr(m, "id", "")})
        reachable = True
        probes.append({
            "name": "models",
            "label": "Daftar model",
            "status": "ok",
            "message": f"{len(available)} model tersedia",
            "latency_ms": ms,
        })
    except asyncio.TimeoutError:
        probes.append({
            "name": "models", "label": "Daftar model", "status": "warn",
            "message": f"Tidak menjawab dalam {int(PROBE_TIMEOUT)} detik",
        })
    except Exception as exc:  # endpoint boleh saja tidak punya /models
        probes.append({
            "name": "models", "label": "Daftar model", "status": "warn",
            "message": f"Endpoint /models tidak tersedia: {_short(exc)}",
        })

    if model_name and available and model_name not in available:
        probes.append({
            "name": "model_name", "label": "Nama model", "status": "warn",
            "message": f"'{model_name}' tidak ada di daftar model endpoint ini",
        })

    # 2. Chat completion kecil — membuktikan model benar-benar bisa dipakai.
    if model_name:
        try:
            resp, ms = await _timed(client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=5,
            ))
            reachable = True
            caps.add("text")
            probes.append({
                "name": "chat", "label": "Uji percakapan", "status": "ok",
                "message": "Model menjawab dengan normal", "latency_ms": ms,
            })
            # Sebagian gateway melaporkan jendela konteks di respons.
            window = getattr(resp, "context_window", None) or getattr(resp, "max_context_length", None)
            if isinstance(window, int) and window > 1024:
                probes.append({
                    "name": "context", "label": "Jendela konteks", "status": "ok",
                    "message": f"{window:,} token dilaporkan endpoint",
                })
        except asyncio.TimeoutError:
            probes.append({
                "name": "chat", "label": "Uji percakapan", "status": "fail",
                "message": f"Tidak menjawab dalam {int(PROBE_TIMEOUT)} detik",
            })
        except Exception as exc:
            probes.append({
                "name": "chat", "label": "Uji percakapan", "status": "fail",
                "message": _short(exc),
            })
    else:
        probes.append({
            "name": "chat", "label": "Uji percakapan", "status": "skip",
            "message": "Nama model belum diisi",
        })

    # 3. Tool calling — fitur wajib untuk mode agen (pencarian web, baca dokumen).
    if model_name:
        try:
            resp, ms = await _timed(client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Berapa cuaca di Jakarta? Gunakan tool."}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Ambil cuaca sebuah kota",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }],
                max_tokens=64,
            ))
            called = bool(resp.choices and getattr(resp.choices[0].message, "tool_calls", None))
            if called:
                caps.add("tools")
                probes.append({
                    "name": "tools", "label": "Pemanggilan tool", "status": "ok",
                    "message": "Model memanggil tool dengan benar", "latency_ms": ms,
                })
            else:
                probes.append({
                    "name": "tools", "label": "Pemanggilan tool", "status": "warn",
                    "message": "Permintaan diterima tapi model tidak memanggil tool — mode agen bisa kurang andal",
                    "latency_ms": ms,
                })
        except asyncio.TimeoutError:
            probes.append({
                "name": "tools", "label": "Pemanggilan tool", "status": "warn",
                "message": f"Tidak menjawab dalam {int(PROBE_TIMEOUT)} detik",
            })
        except Exception as exc:
            probes.append({
                "name": "tools", "label": "Pemanggilan tool", "status": "fail",
                "message": f"Tidak mendukung tool: {_short(exc)}",
            })
    else:
        probes.append({
            "name": "tools", "label": "Pemanggilan tool", "status": "skip",
            "message": "Nama model belum diisi",
        })

    # 4. Vision — kirim gambar 1x1 piksel; endpoint tanpa dukungan akan menolak.
    if model_name:
        try:
            _, ms = await _timed(client.chat.completions.create(
                model=model_name,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "ok?"},
                        {"type": "image_url", "image_url": {"url": _TINY_PNG}},
                    ],
                }],
                max_tokens=5,
            ))
            caps.add("vision")
            probes.append({
                "name": "vision", "label": "Membaca gambar", "status": "ok",
                "message": "Lampiran gambar diterima", "latency_ms": ms,
            })
        except asyncio.TimeoutError:
            probes.append({
                "name": "vision", "label": "Membaca gambar", "status": "warn",
                "message": f"Tidak menjawab dalam {int(PROBE_TIMEOUT)} detik",
            })
        except Exception as exc:
            caps.discard("vision")
            probes.append({
                "name": "vision", "label": "Membaca gambar", "status": "warn",
                "message": f"Tidak menerima gambar: {_short(exc)}",
            })
    else:
        probes.append({
            "name": "vision", "label": "Membaca gambar", "status": "skip",
            "message": "Nama model belum diisi",
        })

    # 5. Embedding — dipakai untuk pencarian di dokumen (RAG).
    if embedding_model:
        try:
            resp, ms = await _timed(client.embeddings.create(
                model=embedding_model, input="uji embedding"
            ))
            dim = len(resp.data[0].embedding) if resp.data else 0
            caps.add("embedding")
            probes.append({
                "name": "embedding", "label": "Model embedding", "status": "ok",
                "message": f"Vektor {dim} dimensi", "latency_ms": ms,
            })
        except asyncio.TimeoutError:
            probes.append({
                "name": "embedding", "label": "Model embedding", "status": "fail",
                "message": f"Tidak menjawab dalam {int(PROBE_TIMEOUT)} detik",
            })
        except Exception as exc:
            probes.append({
                "name": "embedding", "label": "Model embedding", "status": "fail",
                "message": f"Gagal: {_short(exc)} — pencarian dokumen tidak akan jalan",
            })
    else:
        probes.append({
            "name": "embedding", "label": "Model embedding", "status": "skip",
            "message": "Model embedding belum diisi",
        })

    ordered = [c for c in ("text", "vision", "code", "audio", "reasoning", "tools", "embedding") if c in caps]
    return {
        "reachable": reachable,
        "capabilities": ordered,
        "provider_type": guess_provider_type(base_url),
        "context_window": guess_context_window(model_name),
        "available_models": available[:200],
        "probes": probes,
    }
