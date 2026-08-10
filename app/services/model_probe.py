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

    # 3. Model Capability Verification (3 Tahap)
    capability_tier = "tidak_didukung"
    if model_name:
        import json
        passed_stage1 = False
        passed_stage2 = False
        passed_stage3 = False
        
        # Stage 1: Basic Format (Kalkulator)
        try:
            resp, ms = await _timed(client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "Berapa 15 ditambah 27? Gunakan kalkulator."}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "description": "Hitung operasi matematika",
                        "parameters": {
                            "type": "object",
                            "properties": {"expression": {"type": "string"}},
                            "required": ["expression"],
                        },
                    },
                }],
                max_tokens=64,
            ))
            called = bool(resp.choices and getattr(resp.choices[0].message, "tool_calls", None))
            if called:
                passed_stage1 = True
                caps.add("tools")
                probes.append({
                    "name": "stage1_basic", "label": "Tahap 1: Tool Dasar", "status": "ok",
                    "message": "Berhasil memanggil tool", "latency_ms": ms,
                })
            else:
                probes.append({
                    "name": "stage1_basic", "label": "Tahap 1: Tool Dasar", "status": "fail",
                    "message": "Gagal memanggil tool", "latency_ms": ms,
                })
        except asyncio.TimeoutError:
            probes.append({
                "name": "stage1_basic", "label": "Tahap 1: Tool Dasar", "status": "warn",
                "message": f"Tidak menjawab dalam {int(PROBE_TIMEOUT)} detik",
            })
        except Exception as exc:
            probes.append({
                "name": "stage1_basic", "label": "Tahap 1: Tool Dasar", "status": "fail",
                "message": _short(exc),
            })
            
        # Stage 2: Multi-langkah (kalau lolos stage 1)
        if passed_stage1:
            try:
                messages = [
                    {"role": "user", "content": "Cari cuaca di Jakarta hari ini, lalu simpan datanya menggunakan tool simpan_data."},
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "cek_cuaca", "arguments": '{"lokasi":"Jakarta"}'}
                    }]},
                    {"role": "tool", "tool_call_id": "call_1", "content": "Cerah, 32 derajat Celcius"}
                ]
                resp, ms = await _timed(client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "cek_cuaca",
                                "description": "Cek cuaca",
                                "parameters": {"type": "object", "properties": {"lokasi": {"type": "string"}}, "required": ["lokasi"]},
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "simpan_data",
                                "description": "Simpan data",
                                "parameters": {"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]},
                            },
                        }
                    ],
                    max_tokens=64,
                ))
                called = False
                if resp.choices and getattr(resp.choices[0].message, "tool_calls", None):
                    for tc in resp.choices[0].message.tool_calls:
                        if tc.function.name == "simpan_data":
                            called = True
                
                if called:
                    passed_stage2 = True
                    probes.append({
                        "name": "stage2_multi", "label": "Tahap 2: Multi-langkah", "status": "ok",
                        "message": "Berhasil memanggil tool berdasarkan konteks", "latency_ms": ms,
                    })
                else:
                    probes.append({
                        "name": "stage2_multi", "label": "Tahap 2: Multi-langkah", "status": "fail",
                        "message": "Gagal memakai konteks tool sebelumnya", "latency_ms": ms,
                    })
            except Exception as exc:
                probes.append({
                    "name": "stage2_multi", "label": "Tahap 2: Multi-langkah", "status": "fail",
                    "message": _short(exc),
                })
                
        # Stage 3: Skema kompleks (kalau lolos stage 2)
        if passed_stage2:
            try:
                resp, ms = await _timed(client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": "Tulis teks dan berikan sitasi dari google.com dengan judul 'Google'."}],
                    tools=[{
                        "type": "function",
                        "function": {
                            "name": "canvas_write",
                            "description": "Tulis ke kanvas dengan sitasi",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "content": {"type": "string"},
                                    "citations": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "title": {"type": "string"},
                                                "url": {"type": "string"}
                                            },
                                            "required": ["title", "url"]
                                        }
                                    }
                                },
                                "required": ["content", "citations"],
                            },
                        },
                    }],
                    max_tokens=150,
                ))
                
                called = False
                if resp.choices and getattr(resp.choices[0].message, "tool_calls", None):
                    tc = resp.choices[0].message.tool_calls[0]
                    if tc.function.name == "canvas_write":
                        try:
                            args = json.loads(tc.function.arguments)
                            if "citations" in args and isinstance(args["citations"], list) and len(args["citations"]) > 0:
                                passed_stage3 = True
                                called = True
                        except:
                            pass
                            
                if called:
                    probes.append({
                        "name": "stage3_complex", "label": "Tahap 3: Skema Kompleks", "status": "ok",
                        "message": "Berhasil mengisi struktur bersarang", "latency_ms": ms,
                    })
                else:
                    probes.append({
                        "name": "stage3_complex", "label": "Tahap 3: Skema Kompleks", "status": "fail",
                        "message": "Gagal mematuhi skema JSON bersarang", "latency_ms": ms,
                    })
            except Exception as exc:
                probes.append({
                    "name": "stage3_complex", "label": "Tahap 3: Skema Kompleks", "status": "fail",
                    "message": _short(exc),
                })
                
        if passed_stage1 and passed_stage2 and passed_stage3:
            capability_tier = "agentic_penuh_terverifikasi"
        elif passed_stage1 and passed_stage2:
            capability_tier = "agentic_dasar_terverifikasi"
        elif passed_stage1:
            capability_tier = "fallback_react"
            
        if not passed_stage1:
            try:
                resp, ms = await _timed(client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": "Tulis persis 'ACTION: kalkulator(1+1)'."}],
                    max_tokens=20,
                ))
                text_out = resp.choices[0].message.content if resp.choices else ""
                if "ACTION:" in text_out and "kalkulator" in text_out:
                    capability_tier = "fallback_react"
                    probes.append({
                        "name": "stage0_react", "label": "Fallback ReAct", "status": "ok",
                        "message": "Model mendukung format teks terstruktur", "latency_ms": ms,
                    })
            except:
                pass
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
        "capability_tier": capability_tier,
        "context_window": guess_context_window(model_name),
        "available_models": available[:200],
        "probes": probes,
    }
