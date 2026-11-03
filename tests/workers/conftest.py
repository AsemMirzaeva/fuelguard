import pytest

from app.db.sync_db import SessionLocal
from app.workers import tasks
from tests import seed


@pytest.fixture(autouse=True)
def _fresh_database(clean_db):
    """Every worker test starts from empty tables."""


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def world(db):
    return seed.create_world(db, capacity="100")


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Capture .delay() calls so no test can publish to the real broker.

    Each entry is (task_name, tx_id).
    """
    calls: list[tuple] = []
    for task in (tasks.analyze_transaction, tasks.check_declined_attempt, tasks.notify_suspicious_transaction):
        monkeypatch.setattr(task, "delay", lambda *args, _name=task.name: calls.append((_name, *args)))
    return calls
