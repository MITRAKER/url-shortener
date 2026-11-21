import pytest

from app import base62


@pytest.mark.parametrize("n", [0, 1, 61, 62, 12345, 2**40, 2**63 - 1, base62.MAX_VALUE])
def test_round_trip(n: int) -> None:
    assert base62.decode(base62.encode(n)) == n


def test_known_values() -> None:
    assert base62.encode(0) == "0"
    assert base62.encode(61) == "Z"
    assert base62.encode(62) == "10"


def test_largest_value_fits_in_max_length() -> None:
    assert len(base62.encode(base62.MAX_VALUE)) == base62.MAX_LENGTH


@pytest.mark.parametrize("n", [-1, base62.MAX_VALUE + 1])
def test_encode_rejects_out_of_range(n: int) -> None:
    with pytest.raises(ValueError, match="between 0 and"):
        base62.encode(n)


@pytest.mark.parametrize("text", ["", "a" * 12])
def test_decode_rejects_bad_length(text: str) -> None:
    with pytest.raises(ValueError, match="characters long"):
        base62.decode(text)


def test_decode_rejects_bad_characters() -> None:
    with pytest.raises(ValueError, match="not a base62 character"):
        base62.decode("ab-c")
