from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.adapters.ehr import EhrWriteback
from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    Branch,
    Patient,
    Practitioner,
)
from app.services.availability import AvailabilityService
from app.utils.timeutil import ensure_tz, natural_name, now_local

class BookingConflictError(Exception):
    def __init__(self, message: str, alternatives: list[dict] | None = None):
        super().__init__(message)
        self.alternatives = alternatives or []

class BookingService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()
        self.ehr = EhrWriteback(db)
    async def book(
        self,
        *,
        patient_id: UUID,
        practitioner_id: UUID,
        branch_id: UUID,
        starts_at: datetime,
        appointment_type_code: str = "consult",
        idempotency_key: str | None = None,
        notes: str = "",
        full_name_confirmed: str | None = None,
    ) -> dict[str, Any]:
        if not full_name_confirmed and not await self._patient_has_name(patient_id):
            raise ValueError("Caller full name is required before completing a booking.")

        key = idempotency_key or f"book:{patient_id}:{practitioner_id}:{starts_at.isoformat()}"
        existing = await self.db.scalar(select(Appointment).where(Appointment.idempotency_key == key))
        if existing:
            return await self._serialize(existing)

        starts = ensure_tz(starts_at)
        semantic_duplicate = await self.db.scalar(
            select(Appointment).where(
                Appointment.patient_id == patient_id,
                Appointment.practitioner_id == practitioner_id,
                Appointment.starts_at == starts,
                Appointment.status == AppointmentStatus.booked,
            )
        )
        if semantic_duplicate:
            return await self._serialize(semantic_duplicate)

        prac = await self.db.scalar(
            select(Practitioner)
            .options(selectinload(Practitioner.branch))
            .where(Practitioner.id == practitioner_id)
        )
        if not prac:
            raise ValueError("Practitioner not found")
        if prac.branch_id != branch_id:
            raise ValueError(
                f"Branch mismatch: practitioner belongs to {prac.branch.code}, "
                f"but booking requested branch_id={branch_id}"
            )

        appt_type = await self.db.scalar(
            select(AppointmentType).where(AppointmentType.code == appointment_type_code)
        )
        if not appt_type:
            appt_type = await self.db.scalar(select(AppointmentType).limit(1))
        duration = timedelta(minutes=prac.slot_minutes or appt_type.duration_minutes)
        ends = starts + duration

        await self._acquire_write_lock(practitioner_id)
        conflict = await self._find_conflict(practitioner_id, starts, ends, prac.buffer_minutes)
        if conflict:
            alts = await AvailabilityService(self.db).search(
                practitioner_id=practitioner_id,
                date_from=starts.date(),
                date_to=starts.date() + timedelta(days=3),
                limit=3,
            )
            raise BookingConflictError(
                "That slot was just taken. Offering live alternatives.",
                alternatives=alts,
            )

        if starts.date() == now_local().date():
            if starts < now_local() + timedelta(minutes=self.settings.same_day_buffer_minutes):
                raise BookingConflictError(
                    f"Same-day bookings need at least {self.settings.same_day_buffer_minutes} minutes notice."
                )

        if full_name_confirmed:
            patient = await self.db.get(Patient, patient_id)
            if patient:
                patient.full_name = natural_name(full_name_confirmed)

        appt = Appointment(
            id=uuid4(),
            patient_id=patient_id,
            practitioner_id=practitioner_id,
            branch_id=branch_id,
            appointment_type_id=appt_type.id,
            starts_at=starts,
            ends_at=ends,
            status=AppointmentStatus.booked,
            idempotency_key=key,
            notes=notes,
            ehr_writeback_status="pending",
        )
        self.db.add(appt)
        await self.db.flush()
        writeback = await self.ehr.write_appointment(appt, operation="create")
        appt.ehr_writeback_status = writeback["status"]
        appt.ehr_writeback_attempts = 1
        appt.ehr_last_error = writeback.get("error")
        if writeback.get("external_id"):
            appt.cliniko_appointment_id = writeback["external_id"]

        await self.db.commit()
        return await self._serialize(appt, writeback=writeback)
    async def reschedule(
        self,
        *,
        appointment_id: UUID,
        new_starts_at: datetime,
        new_practitioner_id: UUID | None = None,
        new_branch_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        appt = await self.db.scalar(
            select(Appointment)
            .options(selectinload(Appointment.practitioner), selectinload(Appointment.branch))
            .where(Appointment.id == appointment_id)
        )
        if not appt or appt.status == AppointmentStatus.cancelled:
            raise ValueError("Appointment not found or already cancelled")

        fee = self._fee_if_applicable(appt.starts_at, kind="reschedule")
        key = idempotency_key or f"resched:{appointment_id}:{new_starts_at.isoformat()}"
        existing = await self.db.scalar(select(Appointment).where(Appointment.idempotency_key == key))
        if existing:
            return await self._serialize(existing)

        prac_id = new_practitioner_id or appt.practitioner_id
        branch_id = new_branch_id or appt.branch_id
        starts = ensure_tz(new_starts_at)
        prac = await self.db.get(Practitioner, prac_id)
        ends = starts + timedelta(minutes=prac.slot_minutes)

        await self._acquire_write_lock(prac_id)
        conflict = await self._find_conflict(prac_id, starts, ends, prac.buffer_minutes, exclude=appt.id)
        if conflict:
            alts = await AvailabilityService(self.db).search(practitioner_id=prac_id, limit=3)
            raise BookingConflictError("New slot unavailable.", alternatives=alts)

        appt.status = AppointmentStatus.rescheduled
        appt.cancelled_at = now_local()

        new_appt = Appointment(
            id=uuid4(),
            patient_id=appt.patient_id,
            practitioner_id=prac_id,
            branch_id=branch_id,
            appointment_type_id=appt.appointment_type_id,
            starts_at=starts,
            ends_at=ends,
            status=AppointmentStatus.booked,
            idempotency_key=key,
            previous_appointment_id=appt.id,
            fee_charged_inr=fee["amount"],
            fee_reason=fee["reason"],
            notes=f"Rescheduled from {appt.id}",
            ehr_writeback_status="pending",
        )
        self.db.add(new_appt)
        await self.db.flush()

        writeback = await self.ehr.write_appointment(new_appt, operation="reschedule", prior=appt)
        new_appt.ehr_writeback_status = writeback["status"]
        new_appt.ehr_writeback_attempts = 1
        new_appt.ehr_last_error = writeback.get("error")
        if writeback.get("external_id"):
            new_appt.cliniko_appointment_id = writeback["external_id"]

        await self.db.commit()
        result = await self._serialize(new_appt, writeback=writeback)
        result["fee"] = fee
        return result

    async def cancel(
        self,
        *,
        appointment_id: UUID,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        appt = await self.db.get(Appointment, appointment_id)
        if not appt:
            raise ValueError("Appointment not found")
        if appt.status == AppointmentStatus.cancelled:
            return await self._serialize(appt)

        fee = self._fee_if_applicable(appt.starts_at, kind="cancel")
        appt.status = AppointmentStatus.cancelled
        appt.cancelled_at = now_local()
        appt.fee_charged_inr = fee["amount"]
        appt.fee_reason = fee["reason"]
        if reason:
            appt.notes = (appt.notes + f" | cancel: {reason}").strip(" |")

        writeback = await self.ehr.write_appointment(appt, operation="cancel")
        appt.ehr_writeback_status = writeback["status"]
        appt.ehr_writeback_attempts = (appt.ehr_writeback_attempts or 0) + 1
        appt.ehr_last_error = writeback.get("error")
        await self.db.commit()
        result = await self._serialize(appt, writeback=writeback)
        result["fee"] = fee
        return result

    def _fee_if_applicable(self, starts_at: datetime, kind: str) -> dict[str, Any]:
        starts = ensure_tz(starts_at)
        hours = (starts - now_local()).total_seconds() / 3600
        if kind == "reschedule":
            window = self.settings.reschedule_fee_window_hours
            amount = self.settings.reschedule_fee_inr
        else:
            window = self.settings.cancellation_fee_window_hours
            amount = self.settings.cancellation_fee_inr
        if 0 <= hours < window:
            return {
                "applies": True,
                "amount": amount,
                "currency": self.settings.currency,
                "reason": f"{kind}_inside_{window}h_window",
                "message": f"A {self.settings.currency} {amount} {kind} fee applies because the appointment is within {window} hours.",
            }
        return {
            "applies": False,
            "amount": 0,
            "currency": self.settings.currency,
            "reason": None,
            "message": None,
        }

    async def _acquire_write_lock(self, practitioner_id: UUID) -> None:
        if "postgresql" in self.settings.database_url:
            await self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                {"k": str(practitioner_id)},
            )

    async def _find_conflict(
        self,
        practitioner_id: UUID,
        starts: datetime,
        ends: datetime,
        buffer_minutes: int,
        exclude: UUID | None = None,
    ) -> Appointment | None:
        buf = timedelta(minutes=buffer_minutes)
        q = select(Appointment).where(
            and_(
                Appointment.practitioner_id == practitioner_id,
                Appointment.status.in_([AppointmentStatus.booked, AppointmentStatus.rescheduled]),
                Appointment.starts_at < ends + buf,
                Appointment.ends_at > starts - buf,
            )
        )
        if exclude:
            q = q.where(Appointment.id != exclude)
        return await self.db.scalar(q)

    async def _patient_has_name(self, patient_id: UUID) -> bool:
        p = await self.db.get(Patient, patient_id)
        return bool(p and p.full_name and p.full_name.lower() not in {"unknown", "caller", "patient"})

    async def _serialize(self, appt: Appointment, writeback: dict | None = None) -> dict[str, Any]:
        await self.db.refresh(appt)
        patient = await self.db.get(Patient, appt.patient_id)
        prac = await self.db.scalar(
            select(Practitioner).options(selectinload(Practitioner.branch)).where(Practitioner.id == appt.practitioner_id)
        )
        branch = prac.branch if prac else await self.db.get(Branch, appt.branch_id)
        return {
            "appointment_id": str(appt.id),
            "status": appt.status.value,
            "starts_at": ensure_tz(appt.starts_at).isoformat(),
            "ends_at": ensure_tz(appt.ends_at).isoformat(),
            "local_date": ensure_tz(appt.starts_at).date().isoformat(),
            "local_time": ensure_tz(appt.starts_at).strftime("%H:%M"),
            "patient_id": str(appt.patient_id),
            "patient_name": natural_name(patient.full_name) if patient else None,
            "practitioner_id": str(appt.practitioner_id),
            "practitioner_name": prac.display_name if prac else None,
            "branch_id": str(appt.branch_id),
            "branch_code": branch.code if branch else None,
            "branch_name": branch.name if branch else None,
            "fee_charged_inr": appt.fee_charged_inr,
            "fee_reason": appt.fee_reason,
            "currency": self.settings.currency,
            "ehr_writeback": writeback or {"status": appt.ehr_writeback_status, "error": appt.ehr_last_error},
            "idempotency_key": appt.idempotency_key,
        }
