import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from si.config import settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


RESERVE = """
local rate = tonumber(redis.call('GET', KEYS[1]) or '0')
local tokens = tonumber(redis.call('GET', KEYS[2]) or '0')
local cost = tonumber(redis.call('GET', KEYS[3]) or '0')
if rate >= tonumber(ARGV[1]) then return 1 end
if tokens + tonumber(ARGV[2]) > tonumber(ARGV[3]) then return 2 end
if cost + tonumber(ARGV[4]) > tonumber(ARGV[5]) then return 3 end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], 60)
redis.call('INCRBY', KEYS[2], ARGV[2])
redis.call('INCRBYFLOAT', KEYS[3], ARGV[4])
redis.call('EXPIREAT', KEYS[2], ARGV[6])
redis.call('EXPIREAT', KEYS[3], ARGV[6])
return 0
"""
RECONCILE = """
redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('INCRBYFLOAT', KEYS[2], ARGV[2])
return 0
"""

REQUEST_RATE = """
if tonumber(redis.call('GET', KEYS[1]) or '0') >= tonumber(ARGV[1]) then return 1 end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], 60)
return 0
"""


async def check_request_rate(redis: Any, project_id: str) -> None:
    minute = int(datetime.now(timezone.utc).timestamp()) // 60
    try:
        exceeded = await redis.eval(
            REQUEST_RATE, 1, f"si:{{{project_id}}}:http-rate:{minute}", settings.rate_limit,
        )
    except RedisError as exc:
        raise HTTPException(503, "Quota service unavailable") from exc
    if exceeded:
        raise HTTPException(429, "Rate limit exceeded")


@dataclass
class Reservation:
    redis: Any
    keys: list[str]
    tokens: int
    cost: float
    settled: bool = False

    async def reconcile(self, tokens: int, cost: float) -> None:
        if self.settled:
            return
        try:
            await self.redis.eval(RECONCILE, 2, *self.keys[1:],
                                  tokens - self.tokens, cost - self.cost)
            self.settled = True
        except RedisError as exc:
            raise HTTPException(503, "Quota service unavailable") from exc


async def reserve(redis: Any, project_id: str, tokens: int, cost: float) -> Reservation:
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    expiry = calendar.timegm((now.year + (now.month == 12), now.month % 12 + 1, 1, 0, 0, 0))
    keys = [f"si:{{{project_id}}}:rate:{int(now.timestamp()) // 60}",
            f"si:{{{project_id}}}:tokens:{month}", f"si:{{{project_id}}}:cost:{month}"]
    try:
        result = await redis.eval(RESERVE, 3, *keys, settings.rate_limit, tokens,
                                  settings.monthly_tokens, cost, settings.monthly_cost, expiry)
    except RedisError as exc:
        raise HTTPException(503, "Quota service unavailable") from exc
    if result:
        raise HTTPException(429, ["", "Rate limit exceeded", "Monthly token budget exceeded",
                                  "Monthly cost budget exceeded"][int(result)])
    return Reservation(redis, keys, tokens, cost)
