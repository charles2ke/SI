import os
import uuid

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError
from redis.asyncio import Redis

from si.quotas import reserve


async def test_reservation_reconciles_once(redis):
    reservation = await reserve(redis, "project", 100, 0.1)
    await reservation.reconcile(20, 0.02)
    await reservation.reconcile(20, 0.02)
    assert redis.usage[0] == 20
    assert redis.usage[1] == pytest.approx(0.02)


async def test_budget_rejected_atomically(redis):
    with pytest.raises(HTTPException) as exc:
        await reserve(redis, "project", 10**9, 0)
    assert exc.value.status_code == 429
    assert redis.usage == [0, 0]


async def test_fail_closed():
    class Broken:
        async def eval(self, *args):
            raise ConnectionError("offline")
    with pytest.raises(HTTPException) as exc:
        await reserve(Broken(), "project", 1, 0)
    assert exc.value.status_code == 503


@pytest.mark.skipif(not os.getenv("SI_TEST_REDIS_URL"), reason="Requires real Redis")
async def test_real_redis_atomic_reservation():
    redis = Redis.from_url(os.environ["SI_TEST_REDIS_URL"], decode_responses=True)
    reservation = await reserve(redis, str(uuid.uuid4()), 100, 0.1)
    await reservation.reconcile(12, 0.01)
    assert int(await redis.get(reservation.keys[1])) == 12
    assert float(await redis.get(reservation.keys[2])) == pytest.approx(0.01)
    await redis.delete(*reservation.keys)
    await redis.aclose()
