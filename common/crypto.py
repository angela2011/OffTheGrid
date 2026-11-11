"""Ed25519 helpers. Keys and signatures are hex strings so they travel in JSON."""
import hashlib
import json

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey


def generate_keypair():
    sk = SigningKey.generate()
    return sk.encode().hex(), sk.verify_key.encode().hex()


def sign(sk_hex: str, msg: bytes) -> str:
    return SigningKey(bytes.fromhex(sk_hex)).sign(msg).signature.hex()


def verify(pk_hex: str, msg: bytes, sig_hex) -> bool:
    if not sig_hex:
        return False
    try:
        VerifyKey(bytes.fromhex(pk_hex)).verify(msg, bytes.fromhex(sig_hex))
        return True
    except (BadSignatureError, ValueError):
        return False


def canonical(obj) -> bytes:
    """Deterministic JSON so every party signs/hashes identical bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()
