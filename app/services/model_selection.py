"""Resolusi model LLM dari katalog untuk satu percakapan.

Meniru pola Nalar AI (``nalar-ai/services/model_selection``): frontend hanya
mengirim referensi ``{profile_id, model_id}``; kredensial tetap di server dan
baru diresolusi saat percakapan berjalan. Dengan begitu pemilih model di kolom
chat benar-benar menentukan model yang dipakai, bukan sekadar hiasan.
"""

from dataclasses import dataclass, field
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import decrypt_api_key
from app.models.model_config import ModelConfig
from app.services.model_probe import (
    capabilities_from_name,
    guess_context_window,
    guess_provider_type,
)
from app.services.ui_catalog import active_profile_and_model, find_by_id, read_catalog, to_int


class ModelSelectionError(ValueError):
    """Pilihan model tidak bisa diresolusi menjadi konfigurasi yang bisa dipakai."""


# Tier yang benar-benar dipahami oleh app/services/agentic_chat.py.
VALID_CAPABILITY_TIERS = frozenset(
    {
        "tidak_didukung",
        "fallback_react",
        "agentic_dasar_terverifikasi",
        "agentic_penuh_terverifikasi",
    }
)

# Jenis penyedia yang dikenal oleh app/services/model_probe.py.
KNOWN_PROVIDER_TYPES = frozenset({"openai-compatible", "google", "anthropic", "ollama"})


def normalize_capability_tier(value: object) -> str:
    """Tier yang tidak dikenal dianggap belum terverifikasi.

    Konfigurasi lama sempat tersimpan dengan nilai seperti ``basic`` yang tidak
    dipahami siapa pun, sehingga model diperlakukan setengah-setengah. Membaca
    ulang lewat fungsi ini membuat perilakunya jelas tanpa mengubah data.
    """
    text = str(value or "").strip()
    return text if text in VALID_CAPABILITY_TIERS else "tidak_didukung"


def normalize_provider_type(value: object, base_url: str) -> str:
    """Jenis penyedia yang tidak dikenal (mis. ``api``) ditebak dari base URL."""
    text = str(value or "").strip()
    return text if text in KNOWN_PROVIDER_TYPES else guess_provider_type(base_url)


def is_local_embed_model(model_name: str) -> bool:
    """Model embedding lokal (fastembed/ONNX) — tidak butuh base URL atau API key."""
    return (model_name or "").strip().startswith(("local:", "local/"))


@dataclass(frozen=True)
class LLMSelection:
    """Referensi aman ke satu model yang sudah dikonfigurasi."""

    profile_id: str
    model_id: str

    @classmethod
    def from_payload(cls, value: Any) -> "LLMSelection | None":
        if isinstance(value, LLMSelection):
            return value
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ModelSelectionError("Pilihan model tidak valid: harus berupa objek.")

        profile_id = str(value.get("profile_id") or "").strip()
        model_id = str(value.get("model_id") or "").strip()
        if not profile_id and not model_id:
            return None
        if not profile_id or not model_id:
            raise ModelSelectionError(
                "Pilihan model tidak valid: profile_id dan model_id wajib ada."
            )
        return cls(profile_id=profile_id, model_id=model_id)

    def to_dict(self) -> dict[str, str]:
        return {"profile_id": self.profile_id, "model_id": self.model_id}


@dataclass
class ResolvedLLM:
    """Konfigurasi siap pakai untuk membuat client dan menjalankan percakapan."""

    model_name: str
    base_url: str
    api_key: str
    provider_type: str
    context_window: int
    capability_tier: str
    profile_name: str = ""
    capabilities: list[str] = field(default_factory=lambda: ["text"])
    source: str = "catalog"


@dataclass
class ResolvedEmbedding:
    """Konfigurasi siap pakai untuk model embedding."""

    model_name: str
    base_url: str
    api_key: str
    provider_type: str
    profile_name: str = ""
    source: str = "catalog"


def _profile_and_model_for_selection(
    catalog: dict, selection: LLMSelection | None
) -> tuple[dict, dict]:
    """Cari profil & model sesuai pilihan; tanpa pilihan pakai yang aktif."""
    service = catalog.get("services", {}).get("llm") or {}

    if selection is None:
        profile, model = active_profile_and_model(catalog, "llm")
        if profile is None or model is None:
            raise ModelSelectionError(
                "Belum ada model LLM aktif. Atur profil dan model di halaman "
                "Pengaturan, lalu tekan Terapkan."
            )
        return profile, model

    profile = find_by_id(service.get("profiles"), selection.profile_id)
    if profile is None:
        raise ModelSelectionError(
            "Profil model yang dipilih sudah tidak ada di Pengaturan. "
            "Pilih model lain di kolom chat."
        )
    model = find_by_id(profile.get("models"), selection.model_id)
    if model is None:
        raise ModelSelectionError(
            "Model yang dipilih sudah tidak ada pada profil tersebut. "
            "Pilih model lain di kolom chat."
        )
    return profile, model


async def _tier_from_model_configs(
    db: AsyncSession, user_id, model_name: str, base_url: str
) -> tuple[str, list[str]]:
    """Ambil hasil verifikasi (capability_tier) untuk model yang sama.

    Tier hanya boleh berasal dari ``/settings/model/detect``; katalog tidak
    menyimpannya. Kalau model ini belum pernah diverifikasi, kembalikan
    ``tidak_didukung`` sehingga agentic tool tetap dimatikan.
    """
    rows = await db.scalars(select(ModelConfig).where(ModelConfig.user_id == user_id))
    for cfg in rows:
        if cfg.model_name == model_name and (cfg.base_url or "").rstrip("/") == base_url.rstrip("/"):
            try:
                caps = json.loads(cfg.capabilities)
            except (TypeError, ValueError):
                caps = ["text"]
            return (
                normalize_capability_tier(cfg.capability_tier),
                caps if isinstance(caps, list) else ["text"],
            )
    return "tidak_didukung", sorted(capabilities_from_name(model_name))


async def _resolve_from_model_configs(db: AsyncSession, user_id) -> ResolvedLLM:
    """Cadangan untuk user yang katalognya masih kosong (mis. dibuat lewat CLI)."""
    cfg = await db.scalar(
        select(ModelConfig).where(
            ModelConfig.user_id == user_id,
            ModelConfig.is_active == True,  # noqa: E712 — perbandingan kolom SQLAlchemy
        )
    )
    if cfg is None:
        raise ModelSelectionError(
            "Tidak ada konfigurasi model AI yang aktif. Silakan atur dan aktifkan "
            "konfigurasi di halaman Pengaturan."
        )
    try:
        caps = json.loads(cfg.capabilities)
    except (TypeError, ValueError):
        caps = ["text"]
    return ResolvedLLM(
        model_name=cfg.model_name,
        base_url=cfg.base_url,
        api_key=decrypt_api_key(cfg.api_key_encrypted),
        provider_type=normalize_provider_type(cfg.provider_type, cfg.base_url or ""),
        context_window=cfg.context_window,
        capability_tier=normalize_capability_tier(cfg.capability_tier),
        profile_name=cfg.name or "",
        capabilities=caps if isinstance(caps, list) else ["text"],
        source="model_config",
    )


async def resolve_llm(
    db: AsyncSession,
    user_id,
    selection: Any = None,
) -> ResolvedLLM:
    """Resolusi pilihan model menjadi konfigurasi konkret.

    ``selection`` boleh ``None`` (pakai model aktif), dict ``{profile_id,
    model_id}``, atau ``LLMSelection``. Pilihan yang menunjuk profil/model yang
    sudah dihapus memunculkan ``ModelSelectionError`` — bukan diam-diam
    memakai model lain, supaya tidak ada percakapan yang berjalan dengan model
    yang bukan pilihan pengguna.
    """
    resolved_selection = LLMSelection.from_payload(selection)
    catalog = await read_catalog(db, user_id)
    profiles = (catalog.get("services", {}).get("llm") or {}).get("profiles") or []

    if not profiles:
        if resolved_selection is not None:
            raise ModelSelectionError(
                "Katalog model masih kosong. Tambahkan profil dan model di halaman "
                "Pengaturan terlebih dahulu."
            )
        return await _resolve_from_model_configs(db, user_id)

    profile, model = _profile_and_model_for_selection(catalog, resolved_selection)

    model_name = (model.get("model") or "").strip()
    if not model_name:
        raise ModelSelectionError("Model yang dipilih belum punya nama model (field 'model').")

    base_url = (profile.get("base_url") or "").strip()
    if not base_url:
        raise ModelSelectionError(
            f"Profil '{profile.get('name') or profile.get('id')}' belum punya base URL."
        )

    api_key = (profile.get("api_key") or "").strip()
    tier, capabilities = await _tier_from_model_configs(db, user_id, model_name, base_url)

    return ResolvedLLM(
        model_name=model_name,
        base_url=base_url,
        api_key=api_key,
        provider_type=profile.get("binding") or guess_provider_type(base_url),
        context_window=to_int(model.get("context_window"), guess_context_window(model_name)),
        capability_tier=tier,
        profile_name=(profile.get("name") or "").strip(),
        capabilities=capabilities,
        source="catalog",
    )


async def resolve_embedding(
    db: AsyncSession,
    user_id,
) -> ResolvedEmbedding:
    """Resolusi model embedding dari katalog (atau cadangan model_configs).

    Frontend tidak memilih model embedding secara eksplisit; yang dipakai adalah
    profil/model yang *diaktifkan* pada katalog embedding service.
    """
    catalog = await read_catalog(db, user_id)
    profiles = (catalog.get("services", {}).get("embedding") or {}).get("profiles") or []

    if not profiles:
        cfg = await db.scalar(
            select(ModelConfig).where(
                ModelConfig.user_id == user_id,
                ModelConfig.is_active == True,  # noqa: E712
            )
        )
        if cfg is not None and (cfg.embedding_model or "").strip():
            emb_model_name = cfg.embedding_model.strip()
            if is_local_embed_model(emb_model_name):
                return ResolvedEmbedding(
                    model_name=emb_model_name,
                    base_url="",
                    api_key="",
                    provider_type="local",
                    profile_name=cfg.name or "",
                    source="model_config",
                )
            return ResolvedEmbedding(
                model_name=emb_model_name,
                base_url=cfg.base_url,
                api_key=decrypt_api_key(cfg.api_key_encrypted),
                provider_type=normalize_provider_type(cfg.provider_type, cfg.base_url or ""),
                profile_name=cfg.name or "",
                source="model_config",
            )
        raise ModelSelectionError(
            "Belum ada model embedding yang dikonfigurasi. Atur profil embedding "
            "di halaman Pengaturan, lalu tekan Terapkan."
        )

    profile, model = active_profile_and_model(catalog, "embedding")
    if profile is None or model is None:
        raise ModelSelectionError(
            "Belum ada model embedding yang aktif. Atur profil dan model "
            "embedding di halaman Pengaturan, lalu tekan Terapkan."
        )

    model_name = (model.get("model") or "").strip()
    if not model_name:
        raise ModelSelectionError("Model embedding yang aktif belum punya nama model.")

    if is_local_embed_model(model_name):
        return ResolvedEmbedding(
            model_name=model_name,
            base_url="",
            api_key="",
            provider_type="local",
            profile_name=(profile.get("name") or "").strip(),
            source="catalog",
        )

    base_url = (profile.get("base_url") or "").strip()
    if not base_url:
        raise ModelSelectionError(
            f"Profil embedding '{profile.get('name') or profile.get('id')}' belum punya base URL."
        )

    api_key = (profile.get("api_key") or "").strip()
    return ResolvedEmbedding(
        model_name=model_name,
        base_url=base_url,
        api_key=api_key,
        provider_type=profile.get("binding") or guess_provider_type(base_url),
        profile_name=(profile.get("name") or "").strip(),
        source="catalog",
    )


__all__ = [
    "KNOWN_PROVIDER_TYPES",
    "LLMSelection",
    "ModelSelectionError",
    "ResolvedEmbedding",
    "ResolvedLLM",
    "VALID_CAPABILITY_TIERS",
    "is_local_embed_model",
    "normalize_capability_tier",
    "normalize_provider_type",
    "resolve_embedding",
    "resolve_llm",
]
