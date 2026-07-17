from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_settings


def clinic_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now_local() -> datetime:
    return datetime.now(clinic_tz())


def ensure_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=clinic_tz())
    return dt.astimezone(clinic_tz())


def normalize_phone(raw: str | None) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


def natural_name(name: str) -> str:
    if not name:
        return name
    if name.isupper() or name.islower():
        return " ".join(part.capitalize() for part in name.strip().split())
    return name.strip()
