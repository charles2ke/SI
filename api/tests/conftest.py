import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from si.auth import hash_key
from si.db import APIKey, Base, Project, get_session
from si.main import app
from si.quotas import get_redis


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.usage = [0, 0.0]
        self.rate = 0
        self.http_rate = 0

    async def ping(self):
        return True

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **kwargs):
        self.values[key] = value

    async def eval(self, script, count, *args):
        if count == 1:
            if self.http_rate >= args[1]:
                return 1
            self.http_rate += 1
            return 0
        if count == 3:
            rate_limit, tokens, token_limit, cost, cost_limit, _ = args[3:]
            if self.rate >= rate_limit:
                return 1
            if self.usage[0] + tokens > token_limit:
                return 2
            if self.usage[1] + cost > cost_limit:
                return 3
            self.rate += 1
            self.usage[0] += tokens
            self.usage[1] += cost
        else:
            self.usage[0] += args[2]
            self.usage[1] += args[3]
        return 0


@pytest.fixture
def database():
    url = os.getenv("SI_TEST_DATABASE_URL", os.getenv("DATABASE_URL", "sqlite://"))
    options = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool} if url == "sqlite://" else {}
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1), **options)
    if engine.dialect.name == "postgresql":
        from sqlalchemy import text
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([Project(id="test-one", name="One"), Project(id="test-two", name="Two")])
        session.flush()
        session.add_all([
            APIKey(id="admin-key", project_id="test-one", name="admin", key_hash=hash_key("admin"), role="admin"),
            APIKey(id="viewer-key", project_id="test-one", name="viewer", key_hash=hash_key("viewer"), role="viewer"),
            APIKey(id="developer-key", project_id="test-one", name="developer", key_hash=hash_key("developer"), role="developer"),
            APIKey(id="other-key", project_id="test-two", name="other", key_hash=hash_key("other"), role="admin"),
        ])
        session.commit()
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def redis():
    return FakeRedis()


@pytest.fixture
def client(database, redis):
    def session_dependency():
        with database() as session:
            yield session
    app.dependency_overrides[get_session] = session_dependency
    app.dependency_overrides[get_redis] = lambda: redis
    # Schema migration/bootstrap are tested separately; lifespan is deliberately not entered.
    client = TestClient(app, headers={"x-api-key": "admin"})
    yield client
    client.close()
    app.dependency_overrides.clear()
