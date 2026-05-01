from .seed import SeedEncrypt
from .seed import SeedDecrypt
from .seed import SeedRoundKey


def SeedCBCEncrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    round_key = SeedRoundKey(key)
    prev = iv
    ciphertext = bytearray()

    for i in range(0, len(plaintext), 16):
        block = plaintext[i : i + 16]
        xored = bytes(a ^ b for a, b in zip(block, prev))
        encrypted = SeedEncrypt(round_key, xored)
        ciphertext.extend(encrypted)
        prev = encrypted

    return bytes(ciphertext)


def SeedCBCDecrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    round_key = SeedRoundKey(key)
    prev = iv
    plaintext = bytearray()

    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i : i + 16]
        decrypted = SeedDecrypt(round_key, block)
        plain_block = bytes(a ^ b for a, b in zip(decrypted, prev))
        plaintext.extend(plain_block)
        prev = block

    return bytes(plaintext)
