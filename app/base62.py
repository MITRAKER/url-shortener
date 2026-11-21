"""Base62 encoding: turn numbers into short, URL-safe strings.

Alphabet = 0-9, a-z, A-Z (62 characters). Each character carries
log2(62) ~= 5.95 bits, so the 8 characters of a code can represent
62**8 ~= 2.2e14 values (see app/ids.py).
"""

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(ALPHABET)

# We encode unsigned 64-bit numbers. 62**11 > 2**64, so 11 characters always
# suffice. Knowing this bound lets every loop below have a fixed upper limit
# (NASA/JPL rule 2).
MAX_VALUE = 2**64 - 1
MAX_LENGTH = 11


def encode(num: int) -> str:
    """Encode a number in 0..2**64-1 as base62."""
    if not 0 <= num <= MAX_VALUE:
        raise ValueError(f"num must be between 0 and 2**64 - 1, got {num}")
    digits: list[str] = []
    for _ in range(MAX_LENGTH):  # bounded: at most 11 digits, see MAX_LENGTH
        num, remainder = divmod(num, BASE)
        digits.append(ALPHABET[remainder])
        if num == 0:
            break
    else:  # pragma: no cover - unreachable, the range check above guarantees it
        raise ArithmeticError("base62 encoding needed more than MAX_LENGTH digits")
    return "".join(reversed(digits))


def decode(text: str) -> int:
    """Decode a base62 string of 1 to 11 characters back into a number."""
    if not 1 <= len(text) <= MAX_LENGTH:
        raise ValueError(f"text must be 1 to {MAX_LENGTH} characters long, got {len(text)}")
    num = 0
    for char in text:  # bounded by the length check above
        position = ALPHABET.find(char)
        if position < 0:
            raise ValueError(f"{char!r} is not a base62 character")
        num = num * BASE + position
    return num
