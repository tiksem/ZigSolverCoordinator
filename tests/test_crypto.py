"""The frame and image sniffing, against ciphertext made the Kotlin host's way."""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from zigsolver_coordinator.crypto import IV, KEY, decrypt_image, decrypt_message

SNAPSHOT = "*me*\nposition=BB\nhand=A♠K♦\nstack=52.8BB\n"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def seal(data: bytes) -> bytes:
    """AES/CBC/PKCS5PADDING under the shared key, as the Kotlin side does it."""
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def test_an_encrypted_frame_is_opened():
    frame = base64.b64encode(seal(SNAPSHOT.encode())).decode()
    assert decrypt_message(frame) == SNAPSHOT
    assert decrypt_message(f"  {frame}\n") == SNAPSHOT


def test_a_binary_frame_is_opened():
    assert decrypt_message(seal(SNAPSHOT.encode())) == SNAPSHOT
    assert decrypt_message("Indexes: 0,3".encode()) == "Indexes: 0,3"


def test_plaintext_passes_through_untouched():
    for text in ["Indexes: 0,3,7", SNAPSHOT, "New hand #4711", "",
                 "QUJDREVGR0hJSktMTU5PUA==",          # base64 of 16 bytes, not ours
                 "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]:
        assert decrypt_message(text) == text


def test_images_are_sniffed_on_the_png_signature():
    assert decrypt_image(PNG) == PNG
    assert decrypt_image(seal(PNG)) == PNG
    assert decrypt_image(base64.b64encode(seal(PNG))) == PNG
    # Not a PNG however it is read: kept, not dropped — it is still evidence.
    junk = b"not an image at all"
    assert decrypt_image(junk) == junk
