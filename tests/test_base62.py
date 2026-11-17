from app import base62


def test_round_trip():
    for n in [0, 1, 61, 62, 12345, 2**40, 2**63 - 1]:
        assert base62.decode(base62.encode(n)) == n


def test_known_values():
    assert base62.encode(61) == "Z"
    assert base62.encode(62) == "10"


def test_random_code_shape():
    code = base62.random_code(7)
    assert len(code) == 7
    assert all(c in base62.ALPHABET for c in code)
