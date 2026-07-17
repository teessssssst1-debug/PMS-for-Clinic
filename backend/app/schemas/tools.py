from __future__ import annotations
import json
from datetime import date, datetime
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

def _empty_to_none(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str) and v.strip() in {"", "null", "None", "undefined"}:
        return None
    return v

def _as_bool(v: Any, default: bool = False) -> bool:
    v = _empty_to_none(v)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default

def _as_int(v: Any, default: int) -> int:
    v = _empty_to_none(v)
    if v is None:
        return default
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default

def _as_date(v: Any) -> Optional[date]:
    v = _empty_to_none(v)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None

def _as_weekdays(v: Any) -> Optional[list[str]]:
    v = _empty_to_none(v)
    if v is None:
        return None
    if isinstance(v, list):
        out = [str(x).strip().lower() for x in v if str(x).strip()]
        return out or None
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                return _as_weekdays(json.loads(s))
            except json.JSONDecodeError:
                pass
        parts = [p.strip().lower() for p in s.replace(";", ",").split(",") if p.strip()]
        return parts or None
    return None

class ToolEnvelope(BaseModel):
    ok: bool = True
    holding_phrase_hint: str = "One moment while I check that for you."
    data: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None

class StartCallRequest(BaseModel):
    caller_phone: str
    platform_call_id: Optional[str] = None
    direction: str = "inbound"
    @field_validator("platform_call_id", mode="before")
    @classmethod
    def blank_platform(cls, v: Any) -> Any:
        return _empty_to_none(v)
    @field_validator("direction", mode="before")
    @classmethod
    def direction_default(cls, v: Any) -> str:
        v = _empty_to_none(v)
        return v or "inbound"
    @field_validator("caller_phone", mode="before")
    @classmethod
    def phone_str(cls, v: Any) -> str:
        return str(v or "").strip()

class UpdateCallContextRequest(BaseModel):
    context_patch: Optional[dict[str, Any]] = None
    summary: Optional[str] = None
    patient_id: Optional[str] = None
    language: Optional[str] = None
    status: Optional[str] = None
    @field_validator("summary", "patient_id", "language", "status", mode="before")
    @classmethod
    def blank_str(cls, v: Any) -> Any:
        return _empty_to_none(v)
    @field_validator("context_patch", mode="before")
    @classmethod
    def patch_obj(cls, v: Any) -> Any:
        v = _empty_to_none(v)
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return None
        return v

class PatientEnsureRequest(BaseModel):
    phone: str
    full_name: str
    language: str = "en"
    @field_validator("language", mode="before")
    @classmethod
    def lang(cls, v: Any) -> str:
        return _empty_to_none(v) or "en"

class AvailabilityRequest(BaseModel):
    """Tolerant of Bolna quirks: empty strings, string bools/ints, CSV weekdays."""
    branch_code: Optional[str] = None
    branch_id: Optional[str] = None
    practitioner_id: Optional[str] = None
    department_code: Optional[str] = None
    specialty: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    preferred_weekdays: Optional[list[str]] = None
    time_after: Optional[str] = None
    time_before: Optional[str] = None
    day_part: Optional[str] = None
    around_time: Optional[str] = None
    around_window_minutes: int = 90
    earliest_only: bool = False
    same_day: bool = False
    limit: int = 8
    force_fresh: bool = True
    @field_validator(
        "branch_code",
        "branch_id",
        "practitioner_id",
        "department_code",
        "specialty",
        "time_after",
        "time_before",
        "day_part",
        "around_time",
        mode="before",
    )
    @classmethod
    def blank_optional_str(cls, v: Any) -> Any:
        return _empty_to_none(v)

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def coerce_date(cls, v: Any) -> Any:
        return _as_date(v)
    @field_validator("preferred_weekdays", mode="before")
    @classmethod
    def coerce_weekdays(cls, v: Any) -> Any:
        return _as_weekdays(v)
    @field_validator("around_window_minutes", mode="before")
    @classmethod
    def coerce_window(cls, v: Any) -> Any:
        return _as_int(v, 90)

    @field_validator("limit", mode="before")
    @classmethod
    def coerce_limit(cls, v: Any) -> Any:
        return max(1, min(_as_int(v, 8), 20))

    @field_validator("earliest_only", mode="before")
    @classmethod
    def coerce_earliest(cls, v: Any) -> bool:
        return _as_bool(v, False)

    @field_validator("same_day", mode="before")
    @classmethod
    def coerce_same_day(cls, v: Any) -> bool:
        return _as_bool(v, False)

    @field_validator("force_fresh", mode="before")
    @classmethod
    def coerce_fresh(cls, v: Any) -> bool:
        return _as_bool(v, True)
    @model_validator(mode="after")
    def normalize_codes(self) -> "AvailabilityRequest":
        if self.branch_code:
            code = self.branch_code.strip().lower().replace(" ", "_").replace("-", "_")
            aliases = {
                "gurgaon": "gurugram",
                "gurugram_sector_43": "gurugram",
                "sector_43": "gurugram",
                "rajouri": "rajouri_garden",
                "rajouri_garden_delhi": "rajouri_garden",
                "new_delhi": "rajouri_garden",
                "delhi": "rajouri_garden",
                "guwahati": "guwahati",
                "gawahati": "guwahati",
                "gauhati": "guwahati",
                "assam": "guwahati",
                "airport": "guwahati",
                "lgb_airport": "guwahati",
                "guwahati_airport": "guwahati",
                "near_airport": "guwahati",
            }
            object.__setattr__(self, "branch_code", aliases.get(code, code))
        if self.department_code:
            dept = self.department_code.strip().lower().replace(" ", "_")
            dept_aliases = {
                "physio": "physiotherapy",
                "physiotherapy": "physiotherapy",
                "spine_pain": "spine",
                "spine_and_pain": "spine",
                "ortho": "spine",
            }
            object.__setattr__(self, "department_code", dept_aliases.get(dept, dept))
        if self.specialty:
            spec = self.specialty.strip().lower()
            if "physio" in spec:
                object.__setattr__(self, "specialty", "physiotherapy")
            elif "spine" in spec or "ortho" in spec:
                object.__setattr__(self, "specialty", "spine")
        return self

class BookRequest(BaseModel):
    patient_id: Optional[str] = None
    practitioner_id: Optional[str] = None
    branch_id: Optional[str] = None
    # Bolna often invents wrong UUIDs — allow lookup helpers
    branch_code: Optional[str] = None
    practitioner_name: Optional[str] = None
    phone: Optional[str] = None
    starts_at: Optional[datetime] = None
    starts_at_raw: Optional[str] = None  # filled if datetime parse fails upstream
    appointment_type_code: str = "consult"
    full_name_confirmed: Optional[str] = None
    idempotency_key: Optional[str] = None
    notes: str = ""
    session_id: Optional[str] = None
    @field_validator(
        "patient_id",
        "practitioner_id",
        "branch_id",
        "branch_code",
        "practitioner_name",
        "phone",
        "full_name_confirmed",
        "idempotency_key",
        "session_id",
        mode="before",
    )
    @classmethod
    def blank_opt(cls, v: Any) -> Any:
        return _empty_to_none(v)
    @field_validator("appointment_type_code", mode="before")
    @classmethod
    def appt_type(cls, v: Any) -> str:
        return _empty_to_none(v) or "consult"

    @field_validator("notes", mode="before")
    @classmethod
    def notes_str(cls, v: Any) -> str:
        return _empty_to_none(v) or ""

    @field_validator("starts_at", mode="before")
    @classmethod
    def coerce_starts(cls, v: Any) -> Any:
        v = _empty_to_none(v)
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            s = v.strip().replace("Z", "+00:00")
            if " " in s and "T" not in s:
                s = s.replace(" ", "T", 1)
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                return None
        return v


class RescheduleRequest(BaseModel):
    appointment_id: str
    new_starts_at: datetime
    new_practitioner_id: Optional[str] = None
    new_branch_id: Optional[str] = None
    idempotency_key: Optional[str] = None

    @field_validator("new_practitioner_id", "new_branch_id", "idempotency_key", mode="before")
    @classmethod
    def blank_opt(cls, v: Any) -> Any:
        return _empty_to_none(v)


class CancelRequest(BaseModel):
    appointment_id: str
    reason: str = ""
    idempotency_key: Optional[str] = None

    @field_validator("reason", mode="before")
    @classmethod
    def reason_str(cls, v: Any) -> str:
        return _empty_to_none(v) or ""

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def blank_key(cls, v: Any) -> Any:
        return _empty_to_none(v)


class FollowUpRequest(BaseModel):
    caller_phone: str
    reason: str
    details: str
    patient_id: Optional[str] = None
    session_id: Optional[str] = None

    @field_validator("patient_id", "session_id", mode="before")
    @classmethod
    def blank_opt(cls, v: Any) -> Any:
        return _empty_to_none(v)


class MissedOutboundRequest(BaseModel):
    phone: str
    purpose: str
    payload: dict[str, Any] = Field(default_factory=dict)
    patient_id: Optional[str] = None

    @field_validator("patient_id", mode="before")
    @classmethod
    def blank_opt(cls, v: Any) -> Any:
        return _empty_to_none(v)

    @field_validator("payload", mode="before")
    @classmethod
    def payload_obj(cls, v: Any) -> Any:
        v = _empty_to_none(v)
        if v is None:
            return {}
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {}
        return v
