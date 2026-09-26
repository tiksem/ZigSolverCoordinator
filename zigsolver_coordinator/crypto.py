"""AES-256-CBC for what the bot host puts on the wire.

The Kotlin side wraps a frame with

    SecretKeySpec(hashSecret.toByteArray(), "AES")
    Cipher.getInstance("AES/CBC/PKCS5PADDING")

and sends the ciphertext base64'd. Nothing in the frame marks it as encrypted,
and plaintext frames still arrive — `Indexes: 0,3,7` from the index socket, the
snapshot bodies, the log lines in between — so `decrypt_message` sniffs instead
of being told: a frame that isn't base64, isn't a whole number of 16-byte blocks,
doesn't unpad as PKCS#5 or doesn't decode as UTF-8 is handed straight back
untouched. Every one of those checks is on the CIPHERTEXT's own structure, so a
plaintext line only reaches the decoder by looking exactly like a ciphertext,
and even then it has to survive the padding and the UTF-8 pass to be believed.

`encrypt_message` is the way back: what the coordinator itself puts on a
table's socket for the host to act on (the bot's `move(...)` line), sealed
exactly as the host seals its own frames.

`decrypt_image` is the same key over the host's `GET /image/{tableIndex}`, which
is optionally wrapped the same way and equally silent about it. It sniffs too,
on a firmer oracle: text can only be checked for "does this decode", an image
for PNG's signature.
"""
from __future__ import annotations

import base64
import binascii
import re

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = b"u3sZ7Kp1mQ8vT4xN6cR2aW9jF5yH0bLd"
IV = b"G7mQ2vX9pL4sK8dN"

BLOCK = 16

_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def aes_cbc_decrypt(data: bytes | None, key: bytes = KEY, iv: bytes = IV) -> bytes | None:
    """CBC-decrypt and strip the PKCS#5 padding.

    None when the input isn't a whole number of blocks or the padding doesn't
    check out — i.e. "this was never our ciphertext".
    """
    if not data or len(data) % BLOCK:
        return None
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    out = decryptor.update(bytes(data)) + decryptor.finalize()
    pad = out[-1]
    if pad < 1 or pad > BLOCK or pad > len(out):
        return None
    if out[-pad:] != bytes([pad]) * pad:
        return None
    return out[:-pad]


def aes_cbc_encrypt(data: bytes, key: bytes = KEY, iv: bytes = IV) -> bytes:
    """PKCS#5-pad and CBC-encrypt — Kotlin's `AES/CBC/PKCS5PADDING`."""
    padder = padding.PKCS7(BLOCK * 8).padder()
    padded = padder.update(bytes(data)) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def encrypt_message(text: str) -> str:
    """Plain text -> the base64 AES frame the host's `decryptUsing` opens."""
    return base64.b64encode(aes_cbc_encrypt(text.encode("utf-8"))).decode("ascii")


def _utf8(data: bytes | None) -> str | None:
    """Strict UTF-8, or None if the bytes aren't UTF-8 at all."""
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _from_base64(text: str) -> bytes | None:
    """Base64 text -> bytes, or None if it isn't base64 of whole 16-byte blocks."""
    # One AES block is 24 base64 chars; anything shorter can't be a frame of ours.
    if len(text) < 24 or len(text) % 4 or not _BASE64.fullmatch(text):
        return None
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) % BLOCK == 0 else None


def decrypt_message(data: str | bytes) -> str:
    """A frame as it came off the wire -> the text to act on.

    Encrypted (base64 AES-256-CBC under the shared key) -> the plaintext.
    Anything else -> itself, unchanged.
    """
    # Binary frames: the ciphertext raw, with no base64 around it.
    if isinstance(data, (bytes, bytearray, memoryview)):
        raw = bytes(data)
        plain = _utf8(aes_cbc_decrypt(raw))
        if plain is not None:
            return plain
        text = _utf8(raw)
        return text if text is not None else ""

    text = str(data)
    raw = _from_base64(text.strip())
    if raw is None:
        return text
    plain = _utf8(aes_cbc_decrypt(raw))
    return plain if plain is not None else text


def looks_like_png(data: bytes | None) -> bool:
    """Is this a PNG? The only oracle an image response gives us."""
    return bool(data) and bytes(data[: len(PNG_MAGIC)]) == PNG_MAGIC


def decrypt_image(data: bytes) -> bytes:
    """`GET /image/{tableIndex}` as it came off the wire -> the PNG bytes.

    Three shapes, tried in the order they cost:

      1. a plain PNG — a host with no hashSecret configured;
      2. raw AES-256-CBC ciphertext;
      3. that ciphertext base64'd, which is what the socket does.

    Anything else comes back untouched rather than being dropped. The bytes are
    still the evidence we went to fetch, and the API records that they were not
    a PNG — a decrypt that missed is worth seeing, not worth losing.
    """
    raw = bytes(data)
    if looks_like_png(raw):
        return raw

    plain = aes_cbc_decrypt(raw)
    if looks_like_png(plain):
        return plain

    # Base64 is ASCII, so a strict UTF-8 pass over a body that is actually
    # binary fails fast rather than building a megabyte-long string for nothing.
    text = _utf8(raw)
    decoded = _from_base64(text.strip()) if text else None
    unwrapped = aes_cbc_decrypt(decoded) if decoded else None
    if looks_like_png(unwrapped):
        return unwrapped

    return raw
