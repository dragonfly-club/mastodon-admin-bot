import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken


def verify_mastodon_signature(raw_body: bytes, header: str | None, secret: str) -> bool:
    if not header:
        return False
    prefix = "sha256="
    if not header.startswith(prefix):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = header.removeprefix(prefix)
    return hmac.compare_digest(expected, provided)


@dataclass(frozen=True)
class TokenCipher:
    fernet: Fernet

    @classmethod
    def from_key(cls, key: str) -> "TokenCipher":
        return cls(Fernet(key.encode()))

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("stored Mastodon token cannot be decrypted") from exc


def make_state() -> str:
    return secrets.token_urlsafe(32)
