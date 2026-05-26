from __future__ import annotations

import base64
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from price_bot.config import Settings


def load_or_create_private_key(settings: Settings) -> Ed25519PrivateKey:
    private_path = settings.signing_private_key_path
    public_path = settings.signing_public_key_path
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    if private_path.exists():
        return serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    private_path.chmod(0o600)
    return key


def load_public_key(settings: Settings) -> Ed25519PublicKey:
    return serialization.load_pem_public_key(settings.signing_public_key_path.read_bytes())


def sign_bytes(payload: bytes, settings: Settings) -> str:
    key = load_or_create_private_key(settings)
    if not settings.signing_public_key_path.exists():
        settings.signing_public_key_path.write_bytes(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    return base64.b64encode(key.sign(payload)).decode("ascii")


def verify_signature(payload: bytes, signature_b64: str, settings: Settings) -> None:
    public_key = load_public_key(settings)
    try:
        public_key.verify(base64.b64decode(signature_b64), payload)
    except InvalidSignature as exc:
        raise ValueError("Signature verification failed") from exc


def write_signature(path: Path, payload: bytes, settings: Settings) -> Path:
    sig = sign_bytes(payload, settings)
    sig_path = path.with_name(path.name + ".sig")
    sig_path.write_text(sig + "\n")
    return sig_path
