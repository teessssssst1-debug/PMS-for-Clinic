from __future__ import annotations
import asyncio
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import get_settings
from app.db import Base
from app.models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    Branch,
    Department,
    Patient,
    Practitioner,
    PractitionerSchedule,
)
from app.utils.timeutil import normalize_phone


TZ = ZoneInfo("Asia/Kolkata")
IDS = {
    "branch_rg": uuid.UUID("11111111-1111-1111-1111-111111111101"),
    "branch_gg": uuid.UUID("11111111-1111-1111-1111-111111111102"),
    "branch_gw": uuid.UUID("11111111-1111-1111-1111-111111111103"),
    "dept_spine": uuid.UUID("22222222-2222-2222-2222-222222222201"),
    "dept_physio": uuid.UUID("22222222-2222-2222-2222-222222222202"),
    "type_consult": uuid.UUID("33333333-3333-3333-3333-333333333301"),
    "type_followup": uuid.UUID("33333333-3333-3333-3333-333333333302"),
    "prac_isha_rg": uuid.UUID("44444444-4444-4444-4444-444444444401"),
    "prac_shital_rg": uuid.UUID("44444444-4444-4444-4444-444444444402"),
    "prac_disha_gg": uuid.UUID("44444444-4444-4444-4444-444444444403"),
    "prac_nidhi_gg": uuid.UUID("44444444-4444-4444-4444-444444444404"),
    "prac_gautam_gg": uuid.UUID("44444444-4444-4444-4444-444444444405"),
    "prac_anjali_das_gw": uuid.UUID("44444444-4444-4444-4444-444444444406"),
    "prac_rituraj_kalita_gw": uuid.UUID("44444444-4444-4444-4444-444444444407"),
    "prac_nirav_deka_gw": uuid.UUID("44444444-4444-4444-4444-444444444408"),
    "patient_priya": uuid.UUID("55555555-5555-5555-5555-555555555501"),
    "patient_amit": uuid.UUID("55555555-5555-5555-5555-555555555502"),
    "patient_family_a": uuid.UUID("55555555-5555-5555-5555-555555555503"),
    "patient_family_b": uuid.UUID("55555555-5555-5555-5555-555555555504"),
}


def weekday_windows() -> list[tuple[int, time, time]]:
    """Mon–Sat 08:00–20:00 with lunch break; Sunday 09:00–17:00."""
    windows: list[tuple[int, time, time]] = []
    for wd in range(0, 6):  
        windows.append((wd, time(8, 0), time(13, 0)))
        windows.append((wd, time(14, 0), time(20, 0)))
    windows.append((6, time(9, 0), time(13, 0)))  
    windows.append((6, time(14, 0), time(17, 0)))
    return windows


async def seed(session: AsyncSession) -> None:
    existing = await session.scalar(select(Branch).where(Branch.code == "rajouri_garden"))
    if existing:
        print("Seed already present - skipping core entities.")
    else:
        session.add_all(
            [
                Branch(
                    id=IDS["branch_rg"],
                    code="rajouri_garden",
                    name="QI Spine Clinic - Rajouri Garden",
                    city="New Delhi",
                    address="Rajouri Garden, New Delhi, Delhi NCR",
                    phone="8655885566",
                ),
                Branch(
                    id=IDS["branch_gg"],
                    code="gurugram",
                    name="QI Spine Clinic - Gurugram (Sector 43)",
                    city="Gurugram",
                    address="UFF-101, 2nd Floor, The Peach Tree, Block C, Sushant Lok Phase I, Sector 43, Gurugram, Haryana 122002",
                    phone="8108008844",
                ),
                Department(
                    id=IDS["dept_spine"],
                    code="spine",
                    name="Spine & Pain",
                    name_hi="रीढ़ और दर्द विभाग",
                    description="Non-surgical spine care",
                ),
                Department(
                    id=IDS["dept_physio"],
                    code="physiotherapy",
                    name="Physiotherapy",
                    name_hi="फिजियोथेरेपी",
                    description="Musculoskeletal physiotherapy",
                ),
                AppointmentType(
                    id=IDS["type_consult"],
                    code="consult",
                    name="Initial Consultation",
                    name_hi="पहली परामर्श",
                    duration_minutes=30,
                    price_inr=0,  # QI Spine markets first consult free
                ),
                AppointmentType(
                    id=IDS["type_followup"],
                    code="followup",
                    name="Follow-up Session",
                    name_hi="फॉलो-अप सेशन",
                    duration_minutes=30,
                    price_inr=800,
                ),
            ]
        )

        practitioners = [
            Practitioner(
                id=IDS["prac_isha_rg"],
                branch_id=IDS["branch_rg"],
                department_id=IDS["dept_physio"],
                full_name="DR. ISHA GHELANI",
                display_name="Dr. Isha Ghelani",
                title="BPTH, Certified MDT",
                specialties=["physiotherapy", "spine", "back pain", "MDT"],
                slot_minutes=30,
                buffer_minutes=15,
            ),
            Practitioner(
                id=IDS["prac_shital_rg"],
                branch_id=IDS["branch_rg"],
                department_id=IDS["dept_physio"],
                full_name="Dr. Shital Gaikwad",
                display_name="Dr. Shital Gaikwad",
                title="BPTh, MPTh - Musculoskeletal Science",
                specialties=["physiotherapy", "musculoskeletal", "spine"],
                slot_minutes=30,
                buffer_minutes=15,
            ),
            Practitioner(
                id=IDS["prac_disha_gg"],
                branch_id=IDS["branch_gg"],
                department_id=IDS["dept_physio"],
                full_name="Dr. Disha Ashar",
                display_name="Dr. Disha Ashar",
                title="BPTh, MPTh Musculoskeletal science, Certified in MDT",
                specialties=["physiotherapy", "MDT", "spine"],
                slot_minutes=30,
                buffer_minutes=15,
            ),
            Practitioner(
                id=IDS["prac_nidhi_gg"],
                branch_id=IDS["branch_gg"],
                department_id=IDS["dept_physio"],
                full_name="Dr. Nidhi Sanghvi Shah",
                display_name="Dr. Nidhi Sanghvi Shah",
                title="Physiotherapist",
                specialties=["physiotherapy", "spine"],
                slot_minutes=30,
                buffer_minutes=15,
            ),
            Practitioner(
                id=IDS["prac_gautam_gg"],
                branch_id=IDS["branch_gg"],
                department_id=IDS["dept_spine"],
                full_name="Dr. Gautam Shetty",
                display_name="Dr. Gautam Shetty",
                title="MBBS, MS Ortho",
                specialties=["spine", "orthopedics", "pain"],
                slot_minutes=30,
                buffer_minutes=15,
            ),
        ]
        session.add_all(practitioners)

        for prac in practitioners:
            for wd, start, end in weekday_windows():
                session.add(
                    PractitionerSchedule(
                        id=uuid.uuid4(),
                        practitioner_id=prac.id,
                        weekday=wd,
                        start_time=start,
                        end_time=end,
                    )
                )

        session.add_all(
            [
                Patient(
                    id=IDS["patient_priya"],
                    full_name="Priya Sharma",
                    phone=normalize_phone("9876543210"),
                    preferred_language="hi",
                    preferred_branch_id=IDS["branch_rg"],
                    notes="Returning patient - prior physio at Rajouri Garden",
                ),
                Patient(
                    id=IDS["patient_amit"],
                    full_name="Amit Verma",
                    phone=normalize_phone("9876501234"),
                    preferred_language="en",
                    preferred_branch_id=IDS["branch_gg"],
                ),
                Patient(
                    id=IDS["patient_family_a"],
                    full_name="Neha Kapoor",
                    phone=normalize_phone("9988776655"),
                    preferred_language="en",
                ),
                Patient(
                    id=IDS["patient_family_b"],
                    full_name="Rohan Kapoor",
                    phone=normalize_phone("9988776655"),
                    preferred_language="hi",
                ),
            ]
        )
        await session.commit()
        print("Core clinic entities seeded.")

    await seed_guwahati(session)
    prior = await session.scalar(
        select(Appointment).where(
            Appointment.patient_id == IDS["patient_priya"],
            Appointment.status == AppointmentStatus.booked,
        )
    )
    if not prior:
        start = datetime.now(TZ) + timedelta(days=5)
        start = start.replace(hour=11, minute=0, second=0, microsecond=0)
        while start.weekday() > 5:
            start += timedelta(days=1)
        appt = Appointment(
            id=uuid.uuid4(),
            patient_id=IDS["patient_priya"],
            practitioner_id=IDS["prac_isha_rg"],
            branch_id=IDS["branch_rg"],
            appointment_type_id=IDS["type_followup"],
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            status=AppointmentStatus.booked,
            idempotency_key=f"seed-priya-{start.date().isoformat()}",
            notes="Seeded upcoming follow-up",
            ehr_writeback_status="success",
        )
        session.add(appt)
        await session.commit()
        print(f"Seeded returning-patient appointment for Priya at {start.isoformat()}")

    print("Seed complete.")
    print("Branches: rajouri_garden, gurugram, guwahati")
    print("Test phones: 9876543210 (Priya), 9988776655 (family Neha/Rohan), 9876501234 (Amit)")


async def seed_guwahati(session: AsyncSession) -> None:
    existing = await session.scalar(select(Branch).where(Branch.code == "guwahati"))
    if existing:
        print("Guwahati branch already present - skipping.")
        return

    dept_physio = await session.scalar(select(Department).where(Department.code == "physiotherapy"))
    dept_spine = await session.scalar(select(Department).where(Department.code == "spine"))
    if not dept_physio or not dept_spine:
        print("Departments missing - run full seed first.")
        return

    branch = Branch(
        id=IDS["branch_gw"],
        code="guwahati",
        name="QI Spine Clinic - Guwahati (Near International Airport)",
        city="Guwahati",
        address=(
            "Near Lokpriya Gopinath Bordoloi International Airport, "
            "VIP Road / Airport Road area, Guwahati, Assam 781015"
        ),
        phone="03617123456",
    )
    session.add(branch)

    practitioners = [
        Practitioner(
            id=IDS["prac_anjali_das_gw"],
            branch_id=IDS["branch_gw"],
            department_id=dept_physio.id,
            full_name="Dr. Anjali Das",
            display_name="Dr. Anjali Das",
            title="BPTh, MPTh - Musculoskeletal",
            specialties=["physiotherapy", "spine", "back pain"],
            slot_minutes=30,
            buffer_minutes=15,
        ),
        Practitioner(
            id=IDS["prac_rituraj_kalita_gw"],
            branch_id=IDS["branch_gw"],
            department_id=dept_physio.id,
            full_name="Dr. Rituraj Kalita",
            display_name="Dr. Rituraj Kalita",
            title="BPTh, Certified MDT",
            specialties=["physiotherapy", "MDT", "neck pain"],
            slot_minutes=30,
            buffer_minutes=15,
        ),
        Practitioner(
            id=IDS["prac_nirav_deka_gw"],
            branch_id=IDS["branch_gw"],
            department_id=dept_spine.id,
            full_name="Dr. Nirav Deka",
            display_name="Dr. Nirav Deka",
            title="MBBS, MS Ortho",
            specialties=["spine", "orthopedics", "pain"],
            slot_minutes=30,
            buffer_minutes=15,
        ),
    ]
    session.add_all(practitioners)
    for prac in practitioners:
        for wd, start, end in weekday_windows():
            session.add(
                PractitionerSchedule(
                    id=uuid.uuid4(),
                    practitioner_id=prac.id,
                    weekday=wd,
                    start_time=start,
                    end_time=end,
                )
            )
    await session.commit()
    print("Guwahati (Assam) branch seeded with Drs. Das, Kalita, Deka.")

async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        await seed(session)
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
