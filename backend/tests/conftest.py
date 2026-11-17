"""每个测试模块独立 SQLite 库，模拟「刷新/重启后重开」的持久化视角。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


@pytest.fixture(scope="module", autouse=True)
def _client_db(_isolated_db):
    yield


@pytest.fixture(scope="module")
def db_file(_isolated_db):
    return _isolated_db


@pytest.fixture(scope="module")
def _isolated_db(tmp_path_factory):
    """模块级独立数据目录，避免模块间共享 app.db 全局。"""
    data = tmp_path_factory.mktemp("data")

    import app.db as dbmod

    dbmod.DATA_DIR = data
    dbmod.DB_PATH = data / "app.db"
    return dbmod.DB_PATH


@pytest.fixture(scope="module")
def client(db_file):
    import os

    os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
    # 关闭后再次建连即「重开库」，持久化断言因此有据
