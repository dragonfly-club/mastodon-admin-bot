import hashlib
import hmac

from mastodon_admin_bot.security import verify_mastodon_signature


def test_verify_mastodon_signature_accepts_valid_signature() -> None:
    body = b'{"event":"report.created","object":{}}'
    secret = "secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_mastodon_signature(body, f"sha256={digest}", secret)


def test_verify_mastodon_signature_rejects_invalid_signature() -> None:
    assert not verify_mastodon_signature(b"{}", "sha256=bad", "secret")


def test_verify_mastodon_signature_rejects_missing_signature() -> None:
    assert not verify_mastodon_signature(b"{}", None, "secret")
