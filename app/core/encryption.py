"""Enkripsi/dekripsi API key menggunakan Fernet (symmetric encryption).

Key Fernet diturunkan dari SECRET_KEY di settings menggunakan SHA-256,
sehingga tidak perlu menyimpan kunci terpisah.
"""

import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import settings


def _get_fernet() -> Fernet:
    """Buat instance Fernet dari SECRET_KEY."""
    key_bytes = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_api_key(api_key: str) -> str:
    """Enkripsi API key, kembalikan string terenkripsi."""
    return _get_fernet().encrypt(api_key.encode()).decode()


def decrypt_api_key(encrypted: str) -> str:
    """Dekripsi API key yang tersimpan di database."""
    return _get_fernet().decrypt(encrypted.encode()).decode()
