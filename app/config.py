from __future__ import annotations

import os
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EMAIL_RECIPIENT = "dummy1.kim@gmail.com"


def validate_email_mailbox(value: str, field_name: str) -> str:
    """Return a validated envelope address for one configured mailbox."""
    if "\r" in value or "\n" in value:
        raise ValueError(f"{field_name} must not contain newlines")
    if not value.strip():
        raise ValueError(f"{field_name} must contain a valid email address")
    try:
        message = EmailMessage()
        message["To"] = value
        header = message["To"]
        addresses = tuple(header.addresses)
        defects = tuple(header.defects)
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain a valid email address") from exc
    if defects or len(addresses) != 1:
        raise ValueError(f"{field_name} must contain a valid email address")
    return addresses[0].addr_spec


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
    email_recipients: Tuple[str, ...]
    email_from: str
    email_reply_to: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_security: str
    smtp_timeout_sec: float
    email_product_mode: str
    historical_evidence_enabled: bool
    email_max_html_bytes: int
    dart_public_fallback_enabled: bool
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

    @property
    def email_configured(self) -> bool:
        authentication_complete = bool(self.smtp_username) == bool(self.smtp_password)
        structurally_complete = bool(
            self.email_recipients
            and self.email_from.strip()
            and self.smtp_host.strip()
            and self.smtp_port > 0
            and self.smtp_timeout_sec > 0
            and self.smtp_security.strip().lower() in {"starttls", "ssl"}
            and authentication_complete
        )
        if not structurally_complete:
            return False
        try:
            validate_email_mailbox(self.email_from, "EMAIL_FROM")
            if self.email_reply_to:
                validate_email_mailbox(self.email_reply_to, "EMAIL_REPLY_TO")
            for recipient in self.email_recipients:
                validate_email_mailbox(recipient, "EMAIL_TO")
        except ValueError:
            return False
        return True


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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


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
    smtp_port = _env_int("SMTP_PORT", 587)
    smtp_timeout = _env_float("SMTP_TIMEOUT_SEC", 20)
    email_product_mode = os.getenv("EMAIL_PRODUCT_MODE", "v3-facts").strip().lower()
    email_max_html_bytes = _env_int("EMAIL_MAX_HTML_BYTES", 90_000)
    if timeout <= 0 or retries < 0 or backoff < 0:
        raise ValueError(
            "HTTP timeout/retry settings must be non-negative and timeout must be positive"
        )
    if lookback < 0 or lookahead < 0 or default_cash <= 0:
        raise ValueError("IPO cash and KIND date window settings are invalid")
    if smtp_port <= 0 or smtp_timeout <= 0:
        raise ValueError("SMTP port and timeout settings must be positive")
    if email_product_mode not in {"v2", "v3-facts"}:
        raise ValueError("EMAIL_PRODUCT_MODE must be v2 or v3-facts")
    if email_max_html_bytes < 1_000:
        raise ValueError("EMAIL_MAX_HTML_BYTES must be at least 1000")
    configured_recipients = _env_csv("EMAIL_TO", DEFAULT_EMAIL_RECIPIENT)
    if configured_recipients != (DEFAULT_EMAIL_RECIPIENT,):
        raise ValueError(f"EMAIL_TO is fixed to {DEFAULT_EMAIL_RECIPIENT}")
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
        email_recipients=(DEFAULT_EMAIL_RECIPIENT,),
        email_from=os.getenv("EMAIL_FROM", ""),
        email_reply_to=os.getenv("EMAIL_REPLY_TO", ""),
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=smtp_port,
        smtp_username=os.getenv("SMTP_USERNAME", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_security=os.getenv("SMTP_SECURITY", "starttls"),
        smtp_timeout_sec=smtp_timeout,
        email_product_mode=email_product_mode,
        historical_evidence_enabled=_env_bool("HISTORICAL_EVIDENCE_ENABLED", False),
        email_max_html_bytes=email_max_html_bytes,
        dart_public_fallback_enabled=_env_bool("DART_PUBLIC_FALLBACK_ENABLED", True),
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
