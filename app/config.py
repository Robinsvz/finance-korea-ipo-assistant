from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_url: str
    timezone: str
    telegram_bot_token: str
    telegram_chat_id: str
    allowed_telegram_chat_ids: Tuple[str, ...]
    allowed_telegram_user_ids: Tuple[str, ...]
    dart_api_key: str
    naver_client_id: str
    naver_client_secret: str
    default_user_cash: int
    http_timeout_sec: float
    kind_lookback_days: int
    kind_lookahead_days: int
    http_max_retries: int
    http_backoff_base_sec: float
    project_root: Path
    data_dir: Path
    config_dir: Path

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def dart_configured(self) -> bool:
        return bool(self.dart_api_key)

    @property
    def naver_configured(self) -> bool:
        return bool(self.naver_client_id and self.naver_client_secret)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_csv(name: str, default: str = "") -> Tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("sqlite:///data/"):
        relative = database_url.replace("sqlite:///", "", 1)
        return "sqlite:///" + str(PROJECT_ROOT / relative)
    return database_url


def get_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    data_dir = PROJECT_ROOT / "data"
    config_dir = PROJECT_ROOT / "configs"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)

    database_url = normalize_database_url(os.getenv("DATABASE_URL", "sqlite:///data/ipo.db"))
    timezone_name = os.getenv("IPO_TIMEZONE", "Asia/Seoul")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid IPO_TIMEZONE: {timezone_name}") from exc
    timeout = _env_float("HTTP_TIMEOUT_SEC", 20)
    retries = _env_int("HTTP_MAX_RETRIES", 3)
    backoff = _env_float("HTTP_BACKOFF_BASE_SEC", 0.5)
    lookback = _env_int("KIND_LOOKBACK_DAYS", 180)
    lookahead = _env_int("KIND_LOOKAHEAD_DAYS", 365)
    default_cash = _env_int("IPO_DEFAULT_USER_CASH", 10_000_000)
    if timeout <= 0 or retries < 0 or backoff < 0:
        raise ValueError(
            "HTTP timeout/retry settings must be non-negative and timeout must be positive"
        )
    if lookback < 0 or lookahead < 0 or default_cash <= 0:
        raise ValueError("IPO cash and KIND date window settings are invalid")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    allowed_chat_ids = _env_csv("TELEGRAM_ALLOWED_CHAT_IDS", telegram_chat_id)
    return Settings(
        database_url=database_url,
        timezone=timezone_name,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=telegram_chat_id,
        allowed_telegram_chat_ids=allowed_chat_ids,
        allowed_telegram_user_ids=_env_csv("TELEGRAM_REFRESH_ALLOWED_USER_IDS"),
        dart_api_key=os.getenv("DART_API_KEY", ""),
        naver_client_id=os.getenv("NAVER_CLIENT_ID", ""),
        naver_client_secret=os.getenv("NAVER_CLIENT_SECRET", ""),
        default_user_cash=default_cash,
        http_timeout_sec=timeout,
        kind_lookback_days=lookback,
        kind_lookahead_days=lookahead,
        http_max_retries=retries,
        http_backoff_base_sec=backoff,
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        config_dir=config_dir,
    )


def load_yaml_config(filename: str) -> Dict[str, Any]:
    path = get_settings().config_dir / filename
    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"YAML config must contain a mapping: {filename}")
    return loaded
