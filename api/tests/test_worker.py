import pytest
from worker import main


@pytest.mark.asyncio
async def test_shutdown_closes_quota_redis_client(monkeypatch):
    class FakeRedis:
        closed = False

        async def aclose(self):
            self.closed = True

    redis = FakeRedis()
    monkeypatch.setattr(main, "get_redis", lambda: redis)

    await main.shutdown({})

    assert redis.closed
