import os

import pytest
from fastapi.testclient import TestClient

# Tests use TEST_DATABASE_URL if set (CI points it at real Postgres),
# otherwise a throwaway SQLite file.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test.db")

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app, follow_redirects=False) as c:
        yield c
