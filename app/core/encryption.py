from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class SecretBox:
    """Encrypts sensitive values (e.g. BYOK Gemini keys) at rest."""

    def __init__(self, fernet_key: str) -> None:
        if not fernet_key:
            # Dev fallback — generate ephemeral key (not for production).
            fernet_key = Fernet.generate_key().decode()
        self._fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Unable to decrypt secret") from exc


@lru_cache
def get_secret_box() -> SecretBox:
    return SecretBox(get_settings().fernet_key)
