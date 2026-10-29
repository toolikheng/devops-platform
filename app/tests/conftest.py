import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The app reads DATABASE_URL when it is imported, so point it at a throwaway file first.
# Tests must never touch the real app.db.
_db_dir = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_db_dir.name) / 'test.db'}"

from app.main import Base, app, engine  # noqa: E402


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Entering the client runs the lifespan hook (table creation), as uvicorn does.
    with TestClient(app) as client:
        with engine.begin() as conn:  # start every test with empty tables
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
        yield client
