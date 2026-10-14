import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "sqlite:///./si.db").replace("postgresql://", "postgresql+psycopg://", 1))
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    bootstrap_key: str = field(default_factory=lambda: os.getenv(
        "SI_BOOTSTRAP_KEY", "si-local-development"))
    rate_limit: int = field(default_factory=lambda: int(os.getenv("SI_RATE_LIMIT", "120")))
    monthly_tokens: int = field(default_factory=lambda: int(os.getenv("SI_MONTHLY_TOKENS", "1000000")))
    monthly_cost: float = field(default_factory=lambda: float(os.getenv("SI_MONTHLY_COST", "100")))
    cache_ttl: int = field(default_factory=lambda: int(os.getenv("SI_CACHE_TTL", "300")))


settings = Settings()
