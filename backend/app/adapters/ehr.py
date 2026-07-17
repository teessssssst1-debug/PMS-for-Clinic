from __future__ import annotations
from typing import Any
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.adapters.cliniko import ClinikoClient, ClinikoError
from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentType,
    Branch,
    EhrWritebackLog,
    Patient,
    Practitioner,
)

class EhrWriteback:
    """
    Cliniko PMS write-back layer.
    The local database stays the source of truth for the voice agent (fast, always
    available during a live call). Cliniko is the real PMS: confirmed patients and
    appointments are mirrored into Cliniko here.
    Behavior on failure:
    - Booking is still committed locally (source of truth for the voice agent)
    - ehr_writeback_status is set to 'failed' with error text and queued for retry
    - Idempotent retries reuse the same idempotency_key and return the prior success if any
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()

    def _client(self) -> ClinikoClient:
        return ClinikoClient(
            self.settings.cliniko_api_key,
            base_url=self.settings.cliniko_effective_base_url,
            user_agent=self.settings.cliniko_user_agent,
        )

    async def write_appointment(
        self,
        appointment: Appointment,
        *,
        operation: str,
        prior: Appointment | None = None,
    ) -> dict[str, Any]:
        # If we already succeeded for this key, replay success
        prior_ok = await self.db.scalar(
            select(EhrWritebackLog).where(
                EhrWritebackLog.idempotency_key == appointment.idempotency_key,
                EhrWritebackLog.status == "success",
            )
        )
        if prior_ok:
            return {
                "status": "duplicate",
                "external_id": (prior_ok.response_payload or {}).get("external_id"),
                "error": None,
                "message": "Idempotent replay of successful EHR write.",
            }

        payload = await self._build_payload(appointment, operation, prior)
        if not self.settings.cliniko_active:
            return {
                "status": "skipped",
                "external_id": None,
                "error": None,
                "provider": "local_only",
                "message": (
                    "Cliniko is not configured (set CLINIKO_ENABLED=true and CLINIKO_API_KEY). "
                    "Appointment saved locally only."
                ),
            }

        try:
            result = await self._cliniko_write(appointment, payload, operation, prior)

            log = EhrWritebackLog(
                id=uuid4(),
                appointment_id=appointment.id,
                idempotency_key=appointment.idempotency_key,
                request_payload=payload,
                response_payload=result,
                status="success",
            )
            self.db.add(log)
            await self.db.flush()
            return {
                "status": "success",
                "external_id": result.get("external_id") or result.get("id"),
                "error": None,
                "provider": result.get("provider", "cliniko"),
            }
        except Exception as exc:  # noqa: BLE001 — captured for operator follow-up
            log = EhrWritebackLog(
                id=uuid4(),
                appointment_id=appointment.id,
                idempotency_key=appointment.idempotency_key,
                request_payload=payload,
                response_payload=_error_body(exc),
                status="failed",
                error=str(exc),
            )
            self.db.add(log)
            await self.db.flush()
            return {
                "status": "failed",
                "external_id": None,
                "error": str(exc),
                "message": (
                    "Appointment saved locally. Cliniko sync failed and is queued for retry. "
                    "Do not tell the patient the booking failed unless a local conflict occurs."
                ),
            }

    async def _build_payload(
        self, appointment: Appointment, operation: str, prior: Appointment | None
    ) -> dict[str, Any]:
        patient = await self.db.get(Patient, appointment.patient_id)
        prac = await self.db.get(Practitioner, appointment.practitioner_id)
        branch = await self.db.get(Branch, appointment.branch_id)
        appt_type = await self.db.get(AppointmentType, appointment.appointment_type_id)
        return {
            "operation": operation,
            "idempotency_key": appointment.idempotency_key,
            "appointment_id": str(appointment.id),
            "patient": {
                "id": str(patient.id) if patient else None,
                "name": patient.full_name if patient else None,
                "phone": patient.phone if patient else None,
                "cliniko_patient_id": patient.cliniko_patient_id if patient else None,
            },
            "practitioner_id": str(appointment.practitioner_id),
            "cliniko_practitioner_id": prac.cliniko_practitioner_id if prac else None,
            "branch_id": str(appointment.branch_id),
            "cliniko_business_id": branch.cliniko_business_id if branch else None,
            "cliniko_appointment_type_id": (
                appt_type.cliniko_appointment_type_id if appt_type else None
            ),
            "starts_at": appointment.starts_at.isoformat(),
            "ends_at": appointment.ends_at.isoformat(),
            "cliniko_appointment_id": appointment.cliniko_appointment_id,
            "prior_appointment_id": str(prior.id) if prior else None,
            "prior_cliniko_appointment_id": prior.cliniko_appointment_id if prior else None,
        }

    async def _cliniko_write(
        self,
        appointment: Appointment,
        payload: dict[str, Any],
        operation: str,
        prior: Appointment | None,
    ) -> dict[str, Any]:
        client = self._client()

        if operation in ("create", "reschedule"):
            missing = _missing_mappings(payload)
            if missing:
                raise ClinikoError(
                    "Cliniko IDs not mapped for: "
                    + ", ".join(missing)
                    + ". Run scripts/cliniko_provision.py to map businesses, "
                    "practitioners and appointment types."
                )

            if operation == "reschedule" and payload.get("prior_cliniko_appointment_id"):
                try:
                    await client.cancel_individual_appointment(
                        str(payload["prior_cliniko_appointment_id"]),
                        reason="Rescheduled via voice receptionist",
                    )
                except ClinikoError:
                    pass  # keep going; new appointment is what matters

            cliniko_patient_id = await self._ensure_cliniko_patient(client, appointment, payload)

            data = await client.create_individual_appointment(
                patient_id=cliniko_patient_id,
                practitioner_id=str(payload["cliniko_practitioner_id"]),
                business_id=str(payload["cliniko_business_id"]),
                appointment_type_id=str(payload["cliniko_appointment_type_id"]),
                starts_at=appointment.starts_at,
                ends_at=appointment.ends_at,
                notes=f"Voice AI booking {payload['idempotency_key']}",
            )
            ext = str(data.get("id"))
            return {"id": ext, "external_id": ext, "provider": "cliniko"}

        if operation == "cancel":
            ext = payload.get("cliniko_appointment_id")
            if not ext:
                # Nothing to cancel remotely (was never synced) — treat as success.
                return {"id": None, "external_id": None, "provider": "cliniko", "note": "no remote id"}
            await client.cancel_individual_appointment(str(ext), reason="Cancelled via voice receptionist")
            return {"id": str(ext), "external_id": str(ext), "provider": "cliniko"}

        raise ClinikoError(f"Unsupported Cliniko operation: {operation}")

    async def _ensure_cliniko_patient(
        self, client: ClinikoClient, appointment: Appointment, payload: dict[str, Any]
    ) -> str:
        patient_info = payload.get("patient") or {}
        cliniko_patient_id = patient_info.get("cliniko_patient_id")
        if cliniko_patient_id:
            return str(cliniko_patient_id)

        cliniko_patient_id = await client.find_or_create_patient(
            full_name=patient_info.get("name") or "Patient",
            phone=patient_info.get("phone"),
        )
        patient = await self.db.get(Patient, appointment.patient_id)
        if patient and cliniko_patient_id:
            patient.cliniko_patient_id = str(cliniko_patient_id)
            await self.db.flush()
        return str(cliniko_patient_id)


def _missing_mappings(payload: dict[str, Any]) -> list[str]:
    missing = []
    if not payload.get("cliniko_practitioner_id"):
        missing.append("practitioner")
    if not payload.get("cliniko_business_id"):
        missing.append("business/branch")
    if not payload.get("cliniko_appointment_type_id"):
        missing.append("appointment_type")
    return missing


def _error_body(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ClinikoError):
        return {"status": exc.status, "body": exc.body}
    return {}
