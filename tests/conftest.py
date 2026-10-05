from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from app import db
from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    """A throwaway SQLite database at the head migration, used by db.session()."""
    url = f"sqlite:///{tmp_path / 'app.db'}"
    monkeypatch.setattr(db, "settings", Settings(database_url=url))
    db._engine.cache_clear()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    yield url
    db._engine.cache_clear()
