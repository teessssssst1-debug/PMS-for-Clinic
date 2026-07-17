from __future__ import annotations
from datetime import date, datetime, timedelta, time
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentStatus,
    Holiday,
    Practitioner,
    PractitionerSchedule,
)
from app.utils.timeutil import clinic_tz, ensure_tz, now_local


WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

class AvailabilityService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()
        self.tz = clinic_tz()
    async def search(
        self,
        *,
        branch_id: UUID | None = None,
        branch_code: str | None = None,
        practitioner_id: UUID | None = None,
        department_code: str | None = None,
        specialty: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        preferred_weekdays: list[int] | None = None,
        time_after: time | None = None,
        time_before: time | None = None,
        day_part: str | None = None,  # morning|afternoon|evening
        earliest_only: bool = False,
        same_day: bool = False,
        around_time: time | None = None,
        around_window_minutes: int = 90,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return available slots sorted by starts_at. Always fresh from live bookings."""
        today = now_local().date()
        if same_day:
            date_from = today
            date_to = today
        date_from = date_from or today
        date_to = date_to or (date_from + timedelta(days=14))
        practitioners = await self._load_practitioners(
            branch_id=branch_id,
            branch_code=branch_code,
            practitioner_id=practitioner_id,
            department_code=department_code,
            specialty=specialty,
        )
        if not practitioners:
            return []
        holidays = await self._holiday_set(branch_ids={p.branch_id for p in practitioners}, start=date_from, end=date_to)
        busy = await self._busy_map([p.id for p in practitioners], date_from, date_to)
        candidates: list[dict[str, Any]] = []
        day = date_from
        while day <= date_to:
            for prac in practitioners:
                if day in holidays.get(prac.branch_id, set()) or day in holidays.get(None, set()):
                    continue
                weekday = day.weekday()
                if preferred_weekdays is not None and weekday not in preferred_weekdays:
                    continue
                for window_start, window_end in self._windows_for(prac, weekday):
                    slot = datetime.combine(day, window_start, tzinfo=self.tz)
                    end_bound = datetime.combine(day, window_end, tzinfo=self.tz)
                    duration = timedelta(minutes=prac.slot_minutes)
                    buffer = timedelta(minutes=max(prac.buffer_minutes, self.settings.same_day_buffer_minutes if day == today else 0))
                    while slot + duration <= end_bound:
                        slot_end = slot + duration
                        if not self._passes_time_filters(slot, day_part, time_after, time_before, around_time, around_window_minutes):
                            slot += duration + buffer
                            continue
                        if day == today and slot < now_local() + timedelta(minutes=self.settings.same_day_buffer_minutes):
                            slot += duration + buffer
                            continue
                        if self._conflicts(busy.get(prac.id, []), slot, slot_end, buffer):
                            slot += duration + buffer
                            continue
                        candidates.append(self._serialize_slot(prac, slot, slot_end))
                        if earliest_only and candidates:
                            pass
                        slot += duration + buffer
            day += timedelta(days=1)
        candidates.sort(key=lambda s: s["starts_at"])
        if earliest_only:
            return candidates[:1] if candidates else []
        return candidates[:limit]
    async def _load_practitioners(
        self,
        *,
        branch_id: UUID | None,
        branch_code: str | None,
        practitioner_id: UUID | None,
        department_code: str | None,
        specialty: str | None,
    ) -> list[Practitioner]:
        q = (
            select(Practitioner)
            .options(selectinload(Practitioner.branch), selectinload(Practitioner.department), selectinload(Practitioner.schedules))
            .where(Practitioner.is_active.is_(True))
        )
        if practitioner_id:
            q = q.where(Practitioner.id == practitioner_id)
        if branch_id:
            q = q.where(Practitioner.branch_id == branch_id)
        if branch_code:
            from app.models import Branch

            q = q.join(Branch).where(Branch.code == branch_code.lower())
        if department_code:
            from app.models import Department
            q = q.join(Practitioner.department).where(Department.code == department_code.lower())
        if specialty:
            pass
        rows = (await self.db.execute(q)).scalars().unique().all()
        if specialty:
            needle = specialty.lower()
            rows = [p for p in rows if any(needle in str(s).lower() for s in (p.specialties or []))]
        return list(rows)
    async def _holiday_set(
        self, branch_ids: set[UUID], start: date, end: date
    ) -> dict[UUID | None, set[date]]:
        q = select(Holiday).where(and_(Holiday.holiday_date >= start, Holiday.holiday_date <= end))
        rows = (await self.db.execute(q)).scalars().all()
        out: dict[UUID | None, set[date]] = {}
        for h in rows:
            if h.branch_id is None or h.branch_id in branch_ids:
                out.setdefault(h.branch_id, set()).add(h.holiday_date)
        return out
    async def _busy_map(
        self, practitioner_ids: list[UUID], start: date, end: date
    ) -> dict[UUID, list[tuple[datetime, datetime]]]:
        start_dt = datetime.combine(start, time.min, tzinfo=self.tz)
        end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=self.tz)
        q = select(Appointment).where(
            and_(
                Appointment.practitioner_id.in_(practitioner_ids),
                Appointment.status.in_([AppointmentStatus.booked, AppointmentStatus.rescheduled]),
                Appointment.starts_at < end_dt,
                Appointment.ends_at > start_dt,
            )
        )
        rows = (await self.db.execute(q)).scalars().all()
        out: dict[UUID, list[tuple[datetime, datetime]]] = {}
        for a in rows:
            out.setdefault(a.practitioner_id, []).append((ensure_tz(a.starts_at), ensure_tz(a.ends_at)))
        return out
    def _windows_for(self, prac: Practitioner, weekday: int) -> list[tuple[time, time]]:
        return [(s.start_time, s.end_time) for s in prac.schedules if s.weekday == weekday]
    def _passes_time_filters(
        self,
        slot: datetime,
        day_part: str | None,
        time_after: time | None,
        time_before: time | None,
        around_time: time | None,
        around_window_minutes: int,
    ) -> bool:
        t = slot.timetz().replace(tzinfo=None)
        if day_part:
            part = day_part.lower()
            if part == "morning" and not (time(6, 0) <= t < time(12, 0)):
                return False
            if part == "afternoon" and not (time(12, 0) <= t < time(17, 0)):
                return False
            if part == "evening" and not (time(17, 0) <= t <= time(21, 0)):
                return False
        if time_after and t < time_after:
            return False
        if time_before and t > time_before:
            return False
        if around_time:
            around_dt = datetime.combine(slot.date(), around_time, tzinfo=self.tz)
            if abs((slot - around_dt).total_seconds()) > around_window_minutes * 60:
                return False
        return True
    def _conflicts(
        self,
        busy: list[tuple[datetime, datetime]],
        start: datetime,
        end: datetime,
        buffer: timedelta,
    ) -> bool:
        padded_start = start - buffer
        padded_end = end + buffer
        for b_start, b_end in busy:
            if padded_start < b_end and padded_end > b_start:
                return True
        return False
    def _serialize_slot(self, prac: Practitioner, start: datetime, end: datetime) -> dict[str, Any]:
        return {
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            "local_date": start.date().isoformat(),
            "local_time": start.strftime("%H:%M"),
            "practitioner_id": str(prac.id),
            "practitioner_name": prac.display_name,
            "branch_id": str(prac.branch_id),
            "branch_code": prac.branch.code,
            "branch_name": prac.branch.name,
            "department": prac.department.name if prac.department else None,
            "slot_minutes": prac.slot_minutes,
            "buffer_minutes": prac.buffer_minutes,
            "fetched_at": now_local().isoformat(),
            "fresh": True,
        }
def parse_weekdays(values: list[str] | None) -> list[int] | None:
    if not values:
        return None
    out: list[int] = []
    for v in values:
        key = v.strip().lower()
        if key in WEEKDAY_NAMES:
            out.append(WEEKDAY_NAMES[key])
        elif key.isdigit():
            out.append(int(key))
    return out or None
