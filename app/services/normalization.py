from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from app.config import load_yaml_config

BROKER_SUFFIX_PATTERNS = [
    r"\(주\)",
    r"㈜",
    r"주식회사",
    r"증권주식회사",
]


def normalize_company_name(name: str) -> str:
    normalized = name.strip()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = re.sub(r"^\(주\)", "", normalized)
    normalized = re.sub(r"^주식회사", "", normalized)
    return normalized.lower()


def normalize_broker_name(name: str) -> str:
    normalized = name.strip()
    for pattern in BROKER_SUFFIX_PATTERNS:
        normalized = re.sub(pattern, "", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    aliases = {
        "엔에이치투자증권": "NH투자증권",
        "농협투자증권": "NH투자증권",
        "한국투자증권": "한국투자증권",
        "미래에셋증권": "미래에셋증권",
        "신한투자증권": "신한투자증권",
        "하나증권": "하나증권",
        "케이비증권": "KB증권",
        "KB증권": "KB증권",
        "아이비케이투자증권": "IBK투자증권",
        "IBK투자증권": "IBK투자증권",
    }
    try:
        configured = load_yaml_config("brokers.yaml").get("brokers", {})
    except (OSError, ValueError):
        configured = {}
    for canonical, values in configured.items():
        aliases[re.sub(r"\s+", "", canonical)] = canonical
        for alias in values.get("aliases", []):
            cleaned = str(alias)
            for pattern in BROKER_SUFFIX_PATTERNS:
                cleaned = re.sub(pattern, "", cleaned)
            aliases[re.sub(r"\s+", "", cleaned)] = canonical
    return aliases.get(normalized, normalized)


def parse_int(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "N/A", "nan"}:
        return None
    negative_parentheses = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    value = int(float(match.group(0)))
    return -abs(value) if negative_parentheses else value


def parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "N/A", "nan"}:
        return None
    negative_parentheses = text.startswith("(") and text.endswith(")")
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    value = float(match.group(0))
    return -abs(value) if negative_parentheses else value


def parse_date(value: object) -> Optional[date]:
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "-", "N/A", "&nbsp;"}:
        return None
    text = text.replace(".", "-").replace("/", "-")
    match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(0), "%Y-%m-%d").date()
    except ValueError:
        return None


def pct_ratio(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return value / 100.0 if abs(value) > 1 else value
