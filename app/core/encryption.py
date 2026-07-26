"""Enkripsi/dekripsi API key menggunakan Fernet (symmetric encryption).

Key Fernet diturunkan dari SECRET_KEY di settings menggunakan SHA-256,
sehingga tidak perlu menyimpan kunci terpisah.
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    """Buat instance Fernet dari SECRET_KEY."""
    key_bytes = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_api_key(api_key: str) -> str:
    """Enkripsi API key, kembalikan string terenkripsi."""
    return _get_fernet().encrypt((api_key or "").encode()).decode()


def decrypt_api_key(encrypted: str | None) -> str:
    """Dekripsi API key yang tersimpan di database.

    Endpoint lokal (Ollama, LM Studio, llama.cpp) tidak memakai API key, jadi
    nilai kosong adalah kondisi normal — bukan error. Ciphertext yang tidak bisa
    dibuka (misal SECRET_KEY berubah) juga dikembalikan sebagai string kosong
    supaya kegagalannya muncul sebagai 401 dari penyedia model, bukan 500.
    """
    if not encrypted:
        return ""
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except Exception:
        logger.warning("API key tersimpan tidak bisa didekripsi; dianggap kosong.")
        return ""
