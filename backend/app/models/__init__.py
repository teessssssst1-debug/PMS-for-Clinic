from __future__ import annotations
import enum
import uuid
from datetime import date, datetime, time
from typing import Optional
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    Index,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base
TS = text("CURRENT_TIMESTAMP")


class AppointmentStatus(str, enum.Enum):
    booked = "booked"
    rescheduled = "rescheduled"
    cancelled = "cancelled"
    completed = "completed"
    no_show = "no_show"


class FollowUpStatus(str, enum.Enum):
    open = "open"
    contacted = "contacted"
    closed = "closed"


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    city: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    cliniko_business_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS)

    practitioners: Mapped[list["Practitioner"]] = relationship(back_populates="branch")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="branch")


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    name_hi: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")


class Practitioner(Base):
    __tablename__ = "practitioners"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), nullable=False)
    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"), nullable=False)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(128), default="")
    specialties: Mapped[list] = mapped_column(JSON, default=list)
    slot_minutes: Mapped[int] = mapped_column(Integer, default=30)
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=15)
    cliniko_practitioner_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    branch: Mapped[Branch] = relationship(back_populates="practitioners")
    department: Mapped[Department] = relationship()
    schedules: Mapped[list["PractitionerSchedule"]] = relationship(back_populates="practitioner")


class PractitionerSchedule(Base):
    __tablename__ = "practitioner_schedules"
    __table_args__ = (UniqueConstraint("practitioner_id", "weekday", "start_time", name="uq_sched_window"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    practitioner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practitioners.id"), nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon .. 6=Sun
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    practitioner: Mapped[Practitioner] = relationship(back_populates="schedules")


class AppointmentType(Base):
    __tablename__ = "appointment_types"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    name_hi: Mapped[str] = mapped_column(String(128), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    price_inr: Mapped[int] = mapped_column(Integer, default=800)
    cliniko_appointment_type_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (Index("ix_patients_phone", "phone"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    preferred_language: Mapped[str] = mapped_column(String(16), default="en")
    preferred_branch_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("branches.id"), nullable=True)
    cliniko_patient_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS, onupdate=datetime.utcnow)

    appointments: Mapped[list["Appointment"]] = relationship(back_populates="patient")


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appt_prac_start", "practitioner_id", "starts_at"),
        Index("ix_appt_patient", "patient_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("patients.id"), nullable=False)
    practitioner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("practitioners.id"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("branches.id"), nullable=False)
    appointment_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointment_types.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status"), default=AppointmentStatus.booked
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    fee_charged_inr: Mapped[int] = mapped_column(Integer, default=0)
    fee_reason: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    previous_appointment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("appointments.id"), nullable=True
    )
    cliniko_appointment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ehr_writeback_status: Mapped[str] = mapped_column(String(32), default="pending")
    ehr_writeback_attempts: Mapped[int] = mapped_column(Integer, default=0)
    ehr_last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS, onupdate=datetime.utcnow)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    patient: Mapped[Patient] = relationship(back_populates="appointments")
    practitioner: Mapped[Practitioner] = relationship()
    branch: Mapped[Branch] = relationship(back_populates="appointments")
    appointment_type: Mapped[AppointmentType] = relationship()


class CallSession(Base):
    """Persisted call state for drop recovery, callbacks, and outbound context."""

    __tablename__ = "call_sessions"
    __table_args__ = (Index("ix_call_phone_updated", "caller_phone", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caller_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    platform_call_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), default="inbound")
    status: Mapped[str] = mapped_column(String(32), default="active")
    language: Mapped[str] = mapped_column(String(16), default="mixed")
    patient_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("patients.id"), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    parent_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("call_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS, onupdate=datetime.utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboundCampaign(Base):
    __tablename__ = "outbound_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("patients.id"), nullable=True)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    call_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("call_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EhrWritebackLog(Base):
    __tablename__ = "ehr_writeback_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    response_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS)


class FollowUpTicket(Base):
    __tablename__ = "follow_up_tickets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    caller_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    patient_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("patients.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[FollowUpStatus] = mapped_column(
        Enum(FollowUpStatus, name="follow_up_status"), default=FollowUpStatus.open
    )
    call_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("call_sessions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=TS)


class Holiday(Base):
    __tablename__ = "holidays"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("branches.id"), nullable=True)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
