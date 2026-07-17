from __future__ import annotations
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.config import get_settings
from app.models import (
    Appointment,
    AppointmentStatus,
    FollowUpStatus,
    FollowUpTicket,
    OutboundCampaign,
    Patient,
    CallSession,
)
from app.utils.timeutil import natural_name, normalize_phone, now_local

class PatientService:
    def __init__(self, db: AsyncSession):
        self.db = db
    async def lookup_by_phone(self, phone: str) -> dict[str, Any]:
        phone = normalize_phone(phone)
        rows = (
            await self.db.execute(select(Patient).where(Patient.phone == phone).order_by(Patient.created_at))
        ).scalars().all()
        if not rows:
            return {
                "recognized": False,
                "phone": phone,
                "patients": [],
                "needs_name": True,
                "needs_disambiguation": False,
                "message": "New caller. Ask for full name before booking.",
            }
        patients = []
        for p in rows:
            recent = await self._recent_appointments(p.id)
            patients.append(
                {
                    "patient_id": str(p.id),
                    "full_name": natural_name(p.full_name),
                    "preferred_language": p.preferred_language,
                    "preferred_branch_id": str(p.preferred_branch_id) if p.preferred_branch_id else None,
                    "recent_appointments": recent,
                }
            )
        if len(patients) > 1:
            return {
                "recognized": True,
                "phone": phone,
                "patients": patients,
                "needs_name": True,
                "needs_disambiguation": True,
                "message": (
                    "Multiple patients share this phone number (family line). "
                    "Ask for the caller's full name first to disambiguate. "
                    "Do not assume which family member is calling."
                ),
            }
        return {
            "recognized": True,
            "phone": phone,
            "patients": patients,
            "needs_name": True,  # still confirm name before booking per assignment
            "needs_disambiguation": False,
            "message": (
                f"Returning patient likely {patients[0]['full_name']}. "
                "Greet them by name after confirming, and still confirm full name before any booking write."
            ),
        }
    async def get_or_create(self, phone: str, full_name: str, language: str = "en") -> Patient:
        phone = normalize_phone(phone)
        name = natural_name(full_name)
        existing = (
            await self.db.execute(
                select(Patient).where(Patient.phone == phone, Patient.full_name.ilike(name))
            )
        ).scalar_one_or_none()
        if existing:
            return existing
        candidates = (await self.db.execute(select(Patient).where(Patient.phone == phone))).scalars().all()
        for c in candidates:
            if self._names_match(c.full_name, name):
                return c
        patient = Patient(
            id=uuid4(),
            full_name=name,
            phone=phone,
            preferred_language=language[:2] if language else "en",
        )
        self.db.add(patient)
        await self.db.commit()
        await self.db.refresh(patient)
        return patient

    async def _recent_appointments(self, patient_id: UUID, limit: int = 3) -> list[dict]:
        rows = (
            await self.db.execute(
                select(Appointment)
                .options(selectinload(Appointment.practitioner), selectinload(Appointment.branch))
                .where(Appointment.patient_id == patient_id)
                .order_by(desc(Appointment.starts_at))
                .limit(limit)
            )
        ).scalars().all()
        out = []
        for a in rows:
            out.append(
                {
                    "appointment_id": str(a.id),
                    "status": a.status.value,
                    "starts_at": a.starts_at.isoformat(),
                    "practitioner_name": a.practitioner.display_name if a.practitioner else None,
                    "branch_name": a.branch.name if a.branch else None,
                    "branch_code": a.branch.code if a.branch else None,
                }
            )
        return out

    @staticmethod
    def _names_match(a: str, b: str) -> bool:
        ta = {t.lower() for t in a.split() if len(t) > 1}
        tb = {t.lower() for t in b.split() if len(t) > 1}
        if not ta or not tb:
            return False
        return ta == tb or ta.issubset(tb) or tb.issubset(ta)


class CallStateService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()

    async def start_or_resume(
        self,
        *,
        caller_phone: str,
        platform_call_id: str | None = None,
        direction: str = "inbound",
    ) -> dict[str, Any]:
        phone = normalize_phone(caller_phone)
        cutoff = now_local() - timedelta(seconds=self.settings.call_state_ttl_seconds)
        outbound = await self.db.scalar(
            select(OutboundCampaign)
            .where(
                and_(
                    OutboundCampaign.phone == phone,
                    OutboundCampaign.status == "missed",
                    OutboundCampaign.expires_at > now_local(),
                )
            )
            .order_by(desc(OutboundCampaign.created_at))
        )

        prior = await self.db.scalar(
            select(CallSession)
            .where(
                and_(
                    CallSession.caller_phone == phone,
                    CallSession.status.in_(["dropped", "active", "missed_outbound"]),
                    CallSession.updated_at >= cutoff,
                )
            )
            .order_by(desc(CallSession.updated_at))
        )

        patient_lookup = await PatientService(self.db).lookup_by_phone(phone)
        session = CallSession(
            id=uuid4(),
            caller_phone=phone,
            platform_call_id=platform_call_id,
            direction=direction,
            status="active",
            context={},
            parent_session_id=prior.id if prior else None,
        )
        resume_mode = "fresh"
        resume_summary = ""
        ack_hint = None
        if outbound:
            outbound.status = "callback_received"
            session.context["outbound_callback"] = {
                "campaign_id": str(outbound.id),
                "purpose": outbound.purpose,
                "payload": outbound.payload,
            }
            resume_mode = "outbound_callback"
            resume_summary = (
                f"Patient is calling back after a missed outbound call about: {outbound.purpose}. "
                f"Context: {outbound.payload}"
            )
            ack_hint = (
                "Acknowledge briefly that you tried to reach them earlier and continue with that context — "
                "do not restart cold."
            )
            session.summary = resume_summary

        elif prior and prior.status == "dropped":
            session.context = dict(prior.context or {})
            session.context["resumed_from"] = str(prior.id)
            session.patient_id = prior.patient_id
            resume_mode = "dropped_call_recovery"
            resume_summary = prior.summary or str(prior.context)
            ack_hint = (
                "Briefly acknowledge the call dropped, then resume from saved state. "
                "Do not re-ask questions already answered."
            )
            session.summary = f"Resuming after drop. Prior: {resume_summary}"

        elif prior and prior.status == "active":
            session.context = dict(prior.context or {})
            session.parent_session_id = prior.id
            resume_mode = "continue"
            resume_summary = prior.summary or ""

        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        return {
            "session_id": str(session.id),
            "caller_phone": phone,
            "resume_mode": resume_mode,
            "ack_hint": ack_hint,
            "resume_summary": resume_summary,
            "saved_context": session.context,
            "patient_lookup": patient_lookup,
            "today_local": now_local().date().isoformat(),
            "timezone": self.settings.timezone,
            "currency": self.settings.currency,
        }
    async def update_context(
        self,
        session_id: UUID,
        *,
        context_patch: dict[str, Any] | None = None,
        summary: str | None = None,
        patient_id: str | None = None,
        language: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        session = await self.db.get(CallSession, session_id)
        if not session:
            raise ValueError("Call session not found")
        ctx = dict(session.context or {})
        if context_patch:
            ctx.update(context_patch)
            collected = set(ctx.get("collected_fields", []))
            for k, v in context_patch.items():
                if v not in (None, "", [], {}):
                    collected.add(k)
            ctx["collected_fields"] = sorted(collected)
        session.context = ctx
        if summary:
            session.summary = summary
        if patient_id:
            session.patient_id = UUID(patient_id)
        if language:
            session.language = language
        if status:
            session.status = status
            if status in {"dropped", "completed"}:
                session.ended_at = now_local()
        session.updated_at = now_local()
        await self.db.commit()
        return {"session_id": str(session.id), "context": session.context, "status": session.status}
    async def mark_dropped(self, session_id: UUID) -> None:
        await self.update_context(session_id, status="dropped")
    async def create_missed_outbound(
        self,
        *,
        phone: str,
        purpose: str,
        payload: dict[str, Any],
        patient_id: UUID | None = None,
        ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        phone = normalize_phone(phone)
        hours = ttl_hours if ttl_hours is not None else max(
            1, self.settings.outbound_context_ttl_seconds // 3600
        )
        session = CallSession(
            id=uuid4(),
            caller_phone=phone,
            direction="outbound",
            status="missed_outbound",
            patient_id=patient_id,
            context={"purpose": purpose, "payload": payload},
            summary=f"Missed outbound: {purpose}",
            ended_at=now_local(),
        )
        self.db.add(session)
        await self.db.flush()
        campaign = OutboundCampaign(
            id=uuid4(),
            patient_id=patient_id,
            phone=phone,
            purpose=purpose,
            payload=payload,
            status="missed",
            call_session_id=session.id,
            expires_at=now_local() + timedelta(hours=hours),
        )
        self.db.add(campaign)
        await self.db.commit()
        return {"campaign_id": str(campaign.id), "session_id": str(session.id), "status": "missed"}
    async def create_follow_up(
        self,
        *,
        caller_phone: str,
        reason: str,
        details: str,
        patient_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        ticket = FollowUpTicket(
            id=uuid4(),
            caller_phone=normalize_phone(caller_phone),
            patient_id=UUID(patient_id) if patient_id else None,
            reason=reason,
            details=details,
            status=FollowUpStatus.open,
            call_session_id=UUID(session_id) if session_id else None,
        )
        self.db.add(ticket)
        await self.db.commit()
        return {
            "ticket_id": str(ticket.id),
            "status": "open",
            "expectation": (
                "A team member will call back. Do NOT imply a live transfer is happening now."
            ),
        }
