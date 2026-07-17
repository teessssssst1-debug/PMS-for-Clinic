"""
Map Cliniko records into the local database.

The voice agent reads availability from the local DB but writes confirmed
appointments into Cliniko. For that to work, every local Branch / Practitioner /
AppointmentType needs its matching Cliniko id stored locally:

    Branch.cliniko_business_id
    Practitioner.cliniko_practitioner_id
    AppointmentType.cliniko_appointment_type_id

This script:
  1. Fetches businesses, practitioners and appointment types from Cliniko.
  2. Matches them to local rows by (normalized) name / aliases / duration.
  3. Creates Cliniko businesses and appointment types if they don't exist yet.
  4. Writes the resolved Cliniko ids back into the local DB.
  5. Optionally retries recent failed write-backs once mapping is complete.

Run from the backend/ directory:

    python -m scripts.cliniko_provision
    python -m scripts.cliniko_provision --retry-failed

Idempotent: safe to re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import re

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.adapters.cliniko import ClinikoClient, ClinikoError
from app.adapters.ehr import EhrWriteback
from app.config import get_settings
from app.models import Appointment, AppointmentType, Branch, Practitioner


# Local name -> Cliniko default names that come with a trial account.
TYPE_ALIASES = {
    "initial consultation": ["first appointment", "initial consultation", "consult", "new patient"],
    "follow-up session": ["standard appointment", "follow up", "follow-up", "followup", "review"],
    "followup": ["standard appointment", "follow up", "follow-up", "followup"],
    "consult": ["first appointment", "initial consultation", "consult"],
}

BRANCH_ALIASES = {
    "rajouri_garden": ["rajouri", "delhi"],
    "gurugram": ["gurugram", "gurgaon", "sector 43"],
    "guwahati": ["guwahati", "assam", "airport"],
}


def _norm(name: str | None) -> str:
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"\b(dr|doctor|mr|mrs|ms)\.?\b", "", name)
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return " ".join(name.split())


def _cliniko_business_name(item: dict) -> str:
    return item.get("business_name") or item.get("name") or ""


def _cliniko_practitioner_name(item: dict) -> str:
    parts = [item.get("first_name") or "", item.get("last_name") or ""]
    joined = " ".join(p for p in parts if p).strip()
    return joined or item.get("label") or ""


def _match_type(local_name: str, local_code: str, type_by_name: dict[str, dict], types: list[dict]) -> dict | None:
    candidates = [_norm(local_name), _norm(local_code)]
    for key in (local_name, local_code):
        candidates.extend(TYPE_ALIASES.get(_norm(key), []))
    for c in candidates:
        hit = type_by_name.get(_norm(c))
        if hit:
            return hit
    # Duration fallback: prefer 30-min "Standard", else first type.
    by_duration = {t.get("duration_in_minutes"): t for t in types}
    return by_duration.get(30) or (types[0] if types else None)


def _match_business(branch: Branch, businesses: list[dict]) -> dict | None:
    aliases = BRANCH_ALIASES.get(branch.code, []) + [_norm(branch.name), _norm(branch.city), branch.code]
    for b in businesses:
        bn = _norm(_cliniko_business_name(b))
        for a in aliases:
            token = _norm(a)
            if token and token in bn:
                return b
    return None


async def provision(session: AsyncSession, *, retry_failed: bool = False) -> None:
    settings = get_settings()
    if not settings.cliniko_api_key:
        print("CLINIKO_API_KEY is empty. Set it in .env first.")
        return

    client = ClinikoClient(
        settings.cliniko_api_key,
        base_url=settings.cliniko_effective_base_url,
        user_agent=settings.cliniko_user_agent,
    )
    print(f"Cliniko base URL: {client.base_url}")

    businesses = await client.list_all("businesses")
    practitioners = await client.list_all("practitioners")
    appt_types = await client.list_all("appointment_types")
    print(
        f"Fetched from Cliniko: {len(businesses)} businesses, "
        f"{len(practitioners)} practitioners, {len(appt_types)} appointment types."
    )

    type_by_name = {_norm(t.get("name")): t for t in appt_types}
    prac_by_name = {_norm(_cliniko_practitioner_name(p)): p for p in practitioners}

    report: list[str] = []

    # ---- Branches -> businesses ----------------------------------------
    branches = (await session.execute(select(Branch))).scalars().all()
    for br in branches:
        match = _match_business(br, businesses)
        if not match:
            try:
                created = await client.create_business(br.name)
                match = created
                businesses.append(created)
                report.append(f"[business] created in Cliniko: {br.name}")
            except ClinikoError as exc:
                report.append(f"[business] FAILED create {br.name}: {exc}")
                continue
        br.cliniko_business_id = str(match.get("id"))
        report.append(f"[business] {br.code} -> {br.cliniko_business_id}")

    business_ids = [b.cliniko_business_id for b in branches if b.cliniko_business_id]
    practitioner_ids = [str(p.get("id")) for p in practitioners if p.get("id")]

    # ---- Appointment types ---------------------------------------------
    # Prefer aliasing onto Cliniko trial defaults (First / Standard Appointment)
    # instead of creating duplicates. Only create when nothing usable exists.
    types = (await session.execute(select(AppointmentType))).scalars().all()
    for t in types:
        match = _match_type(t.name, t.code, type_by_name, appt_types)
        created_new = False
        if not match:
            try:
                created = await client.create_appointment_type(
                    name=t.name,
                    duration_minutes=t.duration_minutes,
                    business_ids=business_ids or None,
                    practitioner_ids=practitioner_ids or None,
                )
                match = created
                created_new = True
                appt_types.append(created)
                type_by_name[_norm(t.name)] = created
            except ClinikoError as exc:
                report.append(f"[appt_type] FAILED {t.name}: {exc}")
                continue

        t.cliniko_appointment_type_id = str(match.get("id"))
        tag = "created" if created_new else f"mapped<-{match.get('name')}"
        report.append(f"[appt_type] {t.code} ({t.name}) -> {t.cliniko_appointment_type_id} ({tag})")

    # ---- Practitioners -------------------------------------------------
    # Cliniko practitioners are tied to user accounts and cannot be API-created.
    # If the trial account only has one practitioner, map all local doctors to it
    # so write-back works for demos; rename that Cliniko user to a doctor name in the UI if you want.
    pracs = (await session.execute(select(Practitioner))).scalars().all()
    unmatched: list[str] = []
    sole = practitioners[0] if len(practitioners) == 1 else None

    for p in pracs:
        match = (
            prac_by_name.get(_norm(p.full_name))
            or prac_by_name.get(_norm(p.display_name))
        )
        if not match and sole:
            match = sole
            report.append(
                f"[practitioner] {p.display_name} -> sole Cliniko practitioner "
                f"'{_cliniko_practitioner_name(sole)}' id={match.get('id')} (demo fallback)"
            )
        elif not match:
            unmatched.append(p.display_name)
            continue
        else:
            p.cliniko_practitioner_id = str(match.get("id"))
            report.append(f"[practitioner] {p.display_name} -> {p.cliniko_practitioner_id}")
            continue
        p.cliniko_practitioner_id = str(match.get("id"))

    await session.commit()

    print("\n=== Mapping report ===")
    for line in report:
        print(line)

    if unmatched:
        print("\n!! Practitioners not found in Cliniko (create them in the Cliniko UI, then re-run):")
        for name in unmatched:
            print(f"   - {name}")
        print(
            "\nCliniko practitioners are linked to user accounts and cannot be created via the API."
        )
    else:
        print("\nAll practitioners mapped. Cliniko write-back is ready.")

    if retry_failed:
        await _retry_failed_writebacks(session)


async def _retry_failed_writebacks(session: AsyncSession, limit: int = 10) -> None:
    rows = (
        await session.execute(
            select(Appointment)
            .where(Appointment.ehr_writeback_status == "failed")
            .order_by(desc(Appointment.created_at))
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        print("\nNo failed write-backs to retry.")
        return

    print(f"\n=== Retrying {len(rows)} failed write-back(s) ===")
    ehr = EhrWriteback(session)
    for appt in rows:
        # Failed logs do not count as success, so the same idempotency_key can be retried.
        result = await ehr.write_appointment(appt, operation="create")
        appt.ehr_writeback_status = result["status"]
        appt.ehr_writeback_attempts = (appt.ehr_writeback_attempts or 0) + 1
        appt.ehr_last_error = result.get("error")
        if result.get("external_id"):
            appt.cliniko_appointment_id = result["external_id"]
        await session.commit()
        print(
            f"  {appt.starts_at} -> {result['status']} "
            f"cliniko_id={result.get('external_id')} err={result.get('error')}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="After mapping, push recent failed local bookings into Cliniko",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        await provision(session, retry_failed=args.retry_failed)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
