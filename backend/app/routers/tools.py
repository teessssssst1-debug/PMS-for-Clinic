from __future__ import annotations
from datetime import datetime, time
from uuid import UUID
from dateutil import parser as date_parser
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.config import Settings, get_settings
from app.db import get_db
from app.models import Branch, Department, Patient, Practitioner
from app.schemas.tools import (
    AvailabilityRequest,
    BookRequest,
    CancelRequest,
    FollowUpRequest,
    MissedOutboundRequest,
    PatientEnsureRequest,
    RescheduleRequest,
    StartCallRequest,
    ToolEnvelope,
    UpdateCallContextRequest,
)
from app.services.availability import AvailabilityService, parse_weekdays
from app.services.booking import BookingConflictError, BookingService
from app.services.call_state import CallStateService, PatientService
from app.utils.timeutil import clinic_tz, ensure_tz, now_local

router = APIRouter(prefix="/tools", tags=["agent-tools"])

def _auth(x_api_key: str | None, settings: Settings) -> None:
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    parts = value.strip().split(":")
    return time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


def _try_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def UUID_from(value: str) -> UUID:
    return UUID(value)


async def _resolve_practitioner(
    db: AsyncSession,
    practitioner_id: str | None,
    practitioner_name: str | None,
    branch_id: UUID | None,
) -> Practitioner | None:
    uid = _try_uuid(practitioner_id)
    if uid:
        prac = await db.scalar(
            select(Practitioner)
            .options(selectinload(Practitioner.branch), selectinload(Practitioner.department))
            .where(Practitioner.id == uid)
        )
        if prac:
            return prac
    if practitioner_name:
        needle = practitioner_name.strip().lower().replace("dr.", "").replace("dr ", "").strip()
        rows = (
            await db.execute(
                select(Practitioner)
                .options(selectinload(Practitioner.branch), selectinload(Practitioner.department))
                .where(Practitioner.is_active.is_(True))
            )
        ).scalars().unique().all()
        for p in rows:
            if needle in p.display_name.lower() or needle in p.full_name.lower():
                if branch_id is None or p.branch_id == branch_id:
                    return p
        for p in rows:
            if needle in p.display_name.lower() or needle in p.full_name.lower():
                return p
    return None


async def _resolve_branch(
    db: AsyncSession,
    branch_id: str | None,
    branch_code: str | None,
) -> Branch | None:
    uid = _try_uuid(branch_id)
    if uid:
        b = await db.get(Branch, uid)
        if b:
            return b
    if branch_code:
        code = branch_code.strip().lower().replace(" ", "_").replace("-", "_")
        aliases = {
            "gurgaon": "gurugram",
            "gurugram_sector_43": "gurugram",
            "rajouri": "rajouri_garden",
            "delhi": "rajouri_garden",
            "guwahati": "guwahati",
            "gawahati": "guwahati",
            "gauhati": "guwahati",
            "assam": "guwahati",
            "airport": "guwahati",
            "guwahati_airport": "guwahati",
            "near_airport": "guwahati",
        }
        code = aliases.get(code, code)
        return await db.scalar(select(Branch).where(Branch.code == code))
    return None


def _parse_starts_at(raw: dict, body: BookRequest) -> datetime | None:
    if body.starts_at is not None:
        return ensure_tz(body.starts_at)
    for key in ("starts_at", "start_time", "slot_time", "appointment_time"):
        val = raw.get(key)
        if not val or not isinstance(val, str):
            continue
        try:
            dt = date_parser.parse(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=clinic_tz())
            return ensure_tz(dt)
        except (ValueError, TypeError, OverflowError):
            continue
    return None


@router.post("/start_call", response_model=ToolEnvelope)
async def start_call(
    body: StartCallRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    data = await CallStateService(db).start_or_resume(
        caller_phone=body.caller_phone,
        platform_call_id=body.platform_call_id,
        direction=body.direction,
    )
    return ToolEnvelope(
        holding_phrase_hint="Just a second while I pull up your details.",
        data=data,
    )


@router.post("/update_call_context", response_model=ToolEnvelope)
async def update_call_context(
    body: UpdateCallContextRequest,
    session_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    data = await CallStateService(db).update_context(
        UUID_from(session_id),
        context_patch=body.context_patch,
        summary=body.summary,
        patient_id=body.patient_id,
        language=body.language,
        status=body.status,
    )
    return ToolEnvelope(data=data)


@router.get("/lookup_patient", response_model=ToolEnvelope)
async def lookup_patient(
    phone: str,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    data = await PatientService(db).lookup_by_phone(phone)
    return ToolEnvelope(data=data)


@router.post("/ensure_patient", response_model=ToolEnvelope)
async def ensure_patient(
    body: PatientEnsureRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    patient = await PatientService(db).get_or_create(body.phone, body.full_name, body.language)
    return ToolEnvelope(
        data={
            "patient_id": str(patient.id),
            "full_name": patient.full_name,
            "phone": patient.phone,
        }
    )


@router.post("/check_availability", response_model=ToolEnvelope)
async def check_availability(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    """ALWAYS live. Ignore any prior slot list from conversation memory."""
    _auth(x_api_key, settings)
    raw = await request.json()
    if not isinstance(raw, dict):
        raw = {}
    if "branch_code" not in raw and isinstance(raw.get("parameters"), dict):
        raw = raw["parameters"]
    if "branch_code" not in raw and isinstance(raw.get("arguments"), dict):
        raw = raw["arguments"]

    try:
        body = AvailabilityRequest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — return soft error to voice agent
        return ToolEnvelope(
            ok=False,
            holding_phrase_hint="Let me try that availability check again.",
            error=f"Invalid availability request: {exc}",
            data={"received": raw, "hint": "Retry with YYYY-MM-DD dates and branch_code gurugram|rajouri_garden|guwahati"},
        )

    slots = await AvailabilityService(db).search(
        branch_id=UUID_from(body.branch_id) if body.branch_id else None,
        branch_code=body.branch_code,
        practitioner_id=UUID_from(body.practitioner_id) if body.practitioner_id else None,
        department_code=body.department_code,
        specialty=body.specialty,
        date_from=body.date_from,
        date_to=body.date_to,
        preferred_weekdays=parse_weekdays(body.preferred_weekdays),
        time_after=_parse_hhmm(body.time_after),
        time_before=_parse_hhmm(body.time_before),
        day_part=body.day_part,
        around_time=_parse_hhmm(body.around_time),
        around_window_minutes=body.around_window_minutes,
        earliest_only=body.earliest_only,
        same_day=body.same_day,
        limit=body.limit,
    )
    return ToolEnvelope(
        holding_phrase_hint="Let me check live availability for that.",
        data={
            "slots": slots,
            "count": len(slots),
            "query": {
                "branch_code": body.branch_code,
                "department_code": body.department_code,
                "specialty": body.specialty,
                "date_from": body.date_from.isoformat() if body.date_from else None,
                "date_to": body.date_to.isoformat() if body.date_to else None,
                "day_part": body.day_part,
                "around_time": body.around_time,
            },
            "queried_at": now_local().isoformat(),
            "force_fresh": True,
            "instruction": (
                "These slots are live as of queried_at. "
                "If count is 0, widen the search (drop around_time / day_part, or try next day) and call again. "
                "Never invent slots."
            ),
        },
    )


@router.post("/book_appointment", response_model=ToolEnvelope)
async def book_appointment(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    raw = await request.json()
    if not isinstance(raw, dict):
        raw = {}
    if "patient_id" not in raw and isinstance(raw.get("parameters"), dict):
        raw = raw["parameters"]
    if "patient_id" not in raw and isinstance(raw.get("arguments"), dict):
        raw = raw["arguments"]

    try:
        body = BookRequest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        return ToolEnvelope(
            ok=False,
            error=f"Invalid book payload: {exc}",
            data={"received": raw},
            holding_phrase_hint="One moment — I need to reconfirm those booking details.",
        )

    starts = _parse_starts_at(raw, body)
    if starts is None:
        return ToolEnvelope(
            ok=False,
            error="starts_at missing or invalid. Use ISO time from the latest check_availability slot.",
            data={"received": raw},
            holding_phrase_hint="Let me pull the slot again and book it properly.",
        )

    branch = await _resolve_branch(db, body.branch_id, body.branch_code or raw.get("branch_code"))
    prac = await _resolve_practitioner(
        db,
        body.practitioner_id,
        body.practitioner_name or raw.get("practitioner_name") or raw.get("doctor_name"),
        branch.id if branch else None,
    )
    if not prac:
        q = (
            select(Practitioner)
            .options(selectinload(Practitioner.branch), selectinload(Practitioner.department))
            .where(Practitioner.is_active.is_(True))
        )
        if branch:
            q = q.where(Practitioner.branch_id == branch.id)
        rows = (await db.execute(q)).scalars().unique().all()
        physio = [p for p in rows if p.department and p.department.code == "physiotherapy"]
        prac = (physio or rows)[0] if (physio or rows) else None

    if not prac:
        return ToolEnvelope(
            ok=False,
            error="Could not resolve practitioner. Call check_availability again and reuse practitioner_id from the slot.",
            data={"received": raw},
            holding_phrase_hint="Let me check live availability again.",
        )

    branch_id = prac.branch_id
    patient_id = _try_uuid(body.patient_id)
    full_name = body.full_name_confirmed or raw.get("full_name") or raw.get("name")
    phone = body.phone or raw.get("phone") or raw.get("caller_phone")
    if patient_id is None and full_name and phone:
        patient = await PatientService(db).get_or_create(str(phone), str(full_name), "en")
        patient_id = patient.id
        full_name = patient.full_name
    if patient_id is None:
        return ToolEnvelope(
            ok=False,
            error="patient_id missing. Call ensure_patient first with phone + full_name.",
            data={"received": raw},
            holding_phrase_hint="May I confirm your full name once more before I book?",
        )
    if not full_name:
        patient = await db.get(Patient, patient_id)
        full_name = patient.full_name if patient else None
    if not full_name:
        return ToolEnvelope(
            ok=False,
            error="full_name_confirmed is required.",
            data={"received": raw},
            holding_phrase_hint="Could I get your full name to complete the booking?",
        )

    try:
        result = await BookingService(db).book(
            patient_id=patient_id,
            practitioner_id=prac.id,
            branch_id=branch_id,
            starts_at=starts,
            appointment_type_code=body.appointment_type_code or "consult",
            idempotency_key=body.idempotency_key,
            notes=body.notes or "",
            full_name_confirmed=str(full_name),
        )
        sid = _try_uuid(body.session_id)
        if sid:
            try:
                await CallStateService(db).update_context(
                    sid,
                    context_patch={"last_booking": result},
                    summary=f"Booked {result['appointment_id']}",
                    patient_id=str(patient_id),
                )
            except ValueError:
                result["session_tracking"] = "skipped_missing_session"
        return ToolEnvelope(
            holding_phrase_hint="I'm confirming that booking now.",
            data=result,
        )
    except BookingConflictError as exc:
        return ToolEnvelope(
            ok=False,
            holding_phrase_hint="That time just got taken — let me find another.",
            error=str(exc),
            data={"alternatives": exc.alternatives},
        )
    except ValueError as exc:
        return ToolEnvelope(
            ok=False,
            error=str(exc),
            data={"received": raw, "resolved": {
                "practitioner_id": str(prac.id),
                "practitioner_name": prac.display_name,
                "branch_id": str(branch_id),
                "branch_code": prac.branch.code if prac.branch else None,
                "starts_at": starts.isoformat(),
                "patient_id": str(patient_id),
            }},
            holding_phrase_hint="Something didn't match — let me recheck availability.",
        )
    except Exception as exc:  # noqa: BLE001
        return ToolEnvelope(
            ok=False,
            error=f"Booking failed: {exc}",
            data={"received": raw},
            holding_phrase_hint="Sorry, let me try booking that again.",
        )

@router.post("/reschedule_appointment", response_model=ToolEnvelope)
async def reschedule_appointment(
    body: RescheduleRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    try:
        result = await BookingService(db).reschedule(
            appointment_id=UUID_from(body.appointment_id),
            new_starts_at=body.new_starts_at,
            new_practitioner_id=UUID_from(body.new_practitioner_id) if body.new_practitioner_id else None,
            new_branch_id=UUID_from(body.new_branch_id) if body.new_branch_id else None,
            idempotency_key=body.idempotency_key,
        )
        return ToolEnvelope(data=result)
    except BookingConflictError as exc:
        return ToolEnvelope(ok=False, error=str(exc), data={"alternatives": exc.alternatives})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/cancel_appointment", response_model=ToolEnvelope)
async def cancel_appointment(
    body: CancelRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    try:
        result = await BookingService(db).cancel(
            appointment_id=UUID_from(body.appointment_id),
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
        return ToolEnvelope(data=result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/create_follow_up", response_model=ToolEnvelope)
async def create_follow_up(
    body: FollowUpRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    data = await CallStateService(db).create_follow_up(
        caller_phone=body.caller_phone,
        reason=body.reason,
        details=body.details,
        patient_id=body.patient_id,
        session_id=body.session_id,
    )
    return ToolEnvelope(data=data)


@router.post("/simulate_missed_outbound", response_model=ToolEnvelope)
async def simulate_missed_outbound(
    body: MissedOutboundRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    """Test/helper: mark an outbound as unanswered so next inbound is a callback."""
    _auth(x_api_key, settings)
    data = await CallStateService(db).create_missed_outbound(
        phone=body.phone,
        purpose=body.purpose,
        payload=body.payload,
        patient_id=UUID_from(body.patient_id) if body.patient_id else None,
    )
    return ToolEnvelope(data=data)


@router.get("/clinic_directory", response_model=ToolEnvelope)
async def clinic_directory(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    _auth(x_api_key, settings)
    branches = (await db.execute(select(Branch).where(Branch.is_active.is_(True)))).scalars().all()
    departments = (await db.execute(select(Department))).scalars().all()
    practitioners = (
        await db.execute(
            select(Practitioner)
            .options(selectinload(Practitioner.branch), selectinload(Practitioner.department))
            .where(Practitioner.is_active.is_(True))
        )
    ).scalars().unique().all()
    return ToolEnvelope(
        data={
            "clinic_name": "QI Spine Clinic (Voice Demo Integration)",
            "timezone": settings.timezone,
            "currency": settings.currency,
            "branches": [
                {
                    "id": str(b.id),
                    "code": b.code,
                    "name": b.name,
                    "city": b.city,
                    "address": b.address,
                    "phone": b.phone,
                }
                for b in branches
            ],
            "departments": [
                {"code": d.code, "name": d.name, "name_hi": d.name_hi} for d in departments
            ],
            "practitioners": [
                {
                    "id": str(p.id),
                    "name": p.display_name,
                    "title": p.title,
                    "branch_code": p.branch.code,
                    "branch_name": p.branch.name,
                    "department": p.department.name,
                    "specialties": p.specialties,
                    "slot_minutes": p.slot_minutes,
                    "buffer_minutes": p.buffer_minutes,
                }
                for p in practitioners
            ],
            "policy": {
                "same_day_buffer_minutes": settings.same_day_buffer_minutes,
                "reschedule_fee_inr": settings.reschedule_fee_inr,
                "reschedule_fee_window_hours": settings.reschedule_fee_window_hours,
                "cancellation_fee_inr": settings.cancellation_fee_inr,
                "cancellation_fee_window_hours": settings.cancellation_fee_window_hours,
                "fee_rule": "Mention fees ONLY when the change falls inside the fee window.",
            },
            "hours": "Monday–Saturday 8:00–20:00, Sunday 9:00–17:00 (Asia/Kolkata)",
            "source": "https://www.qispine.com/ — public branch/doctor listings adapted for Cliniko-compatible scheduling demo",
        }
    )
