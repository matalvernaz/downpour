"""Shared fixtures: an isolated data directory, a test server, engines."""

from __future__ import annotations

import pytest

from tests.helpers import Server


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DOWNPOUR_HOME", str(home))
    return home


@pytest.fixture
def server():
    srv = Server()
    yield srv
    srv.close()


@pytest.fixture
def fast_retry(monkeypatch):
    from downpour.core import task

    monkeypatch.setattr(task, "backoff", lambda failures: 0.05)


@pytest.fixture
def make_engine(tmp_path):
    from downpour.core.engine import Engine
    from downpour.core.settings import Settings
    from downpour.core.store import Store

    engines = []

    def factory(**overrides):
        settings = Settings(
            download_dir=str(tmp_path / "downloads"),
            use_category_folders=False,
            min_split_size=64 * 1024,
            timeout=5.0,
            max_retries=5,
            recycle_bin=False,  # tests must not fill the real Recycle Bin
        )
        for key, value in overrides.items():
            setattr(settings, key, value)
        engine = Engine(Store(tmp_path / "test.db"), settings)
        engines.append(engine)
        return engine

    yield factory
    for engine in engines:
        try:
            engine.shutdown(timeout=5)
        except Exception:  # noqa: BLE001 - already shut down
            pass
