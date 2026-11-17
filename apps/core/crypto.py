"""
Encryption at rest for secrets we store (Shopify access/refresh tokens).

Uses Fernet (AES-128-CBC + HMAC). `ENCRYPTION_KEY` may hold several keys
separated by commas: the FIRST encrypts, ALL are tried when decrypting. To rotate:
put the new key first, keep the old one after it, run
`python manage.py rotate_encryption` (re-encrypts), then remove the old key.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class DecryptionError(Exception):
    """Stored ciphertext could not be decrypted with any configured key."""


@lru_cache(maxsize=4)
def _fernet(raw_keys: str) -> MultiFernet:
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if not keys:
        raise ImproperlyConfigured(
            "ENCRYPTION_KEY is empty. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt(plaintext: str) -> str:
    return _fernet(settings.ENCRYPTION_KEY).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet(settings.ENCRYPTION_KEY).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError("Could not decrypt value; was ENCRYPTION_KEY changed?") from exc


def rotate(ciphertext: str) -> str:
    """Re-encrypt with the first (newest) key."""
    return _fernet(settings.ENCRYPTION_KEY).rotate(ciphertext.encode()).decode()
