"""Base62 encoding: turn numbers into short, URL-safe strings.

Alphabet = 0-9, a-z, A-Z (62 characters). Each character carries
log2(62) ~= 5.95 bits, so 7 characters can represent 62**7 ~= 3.5e12 values.
"""
import secrets

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(ALPHABET)


def encode(num: int) -> str:
    if num < 0:
        raise ValueError("num must be non-negative")
    if num == 0:
        return ALPHABET[0]
    chars = []
    while num:
        num, rem = divmod(num, BASE)
        chars.append(ALPHABET[rem])
    return "".join(reversed(chars))


def decode(text: str) -> int:
    num = 0
    for ch in text:
        num = num * BASE + ALPHABET.index(ch)
    return num


def random_code(length: int) -> str:
    """A random code of exactly `length` characters (cryptographically random,
    so codes cannot be guessed by counting)."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
