from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import httpx


def shard_from_api_key(api_key: str) -> str | None:
    if not api_key or "-" not in api_key:
        return None
    return api_key.rsplit("-", 1)[-1].strip() or None


def base_url_for_key(api_key: str, fallback: str) -> str:
    shard = shard_from_api_key(api_key)
    if shard:
        return f"https://api.{shard}.cliniko.com/v1"
    return fallback


def to_cliniko_utc(dt: datetime) -> str:
    """Cliniko expects UTC ISO-8601 with a trailing Z.

    Naive datetimes from SQLite are treated as Asia/Kolkata (clinic local),
    not UTC — otherwise a 2pm booking is written as 14:00Z.
    """
    from zoneinfo import ZoneInfo

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClinikoError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ClinikoClient:
    def __init__(self, api_key: str, *, base_url: str, user_agent: str, timeout: float = 20.0):
        self.api_key = api_key
        self.base_url = base_url_for_key(api_key, base_url)
        self.user_agent = user_agent
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    @property
    def _auth(self) -> tuple[str, str]:
        return (self.api_key, "")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            resp = await client.request(
                method, path, headers=self._headers, auth=self._auth, **kwargs
            )
        if resp.status_code >= 400:
            body = _safe_json(resp)
            raise ClinikoError(
                f"Cliniko {method} {path} -> {resp.status_code}: {body}",
                status=resp.status_code,
                body=body,
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def find_patient_by_name(self, *, first_name: str, last_name: str) -> dict[str, Any] | None:
        if not first_name:
            return None
        # Phone is NOT filterable on Cliniko patients — name only.
        q = [f"first_name:={first_name}"]
        if last_name:
            q.append(f"last_name:={last_name}")
        data = await self._request("GET", "/patients", params={"q[]": q, "per_page": 5})
        patients = data.get("patients") or []
        return patients[0] if patients else None

    async def create_patient(self, *, full_name: str, phone: str | None) -> dict[str, Any]:
        parts = (full_name or "Patient").split()
        first = parts[0] if parts else "Patient"
        last = " ".join(parts[1:]) or "Patient"
        body: dict[str, Any] = {"first_name": first, "last_name": last}
        if phone:
            body["patient_phone_numbers"] = [{"number": phone, "phone_type": "Mobile"}]
        return await self._request("POST", "/patients", json=body)

    async def find_or_create_patient(self, *, full_name: str, phone: str | None) -> str:
        parts = (full_name or "Patient").split()
        first = parts[0] if parts else "Patient"
        last = " ".join(parts[1:]) or "Patient"
        found = await self.find_patient_by_name(first_name=first, last_name=last)
        if found and found.get("id"):
            return str(found["id"])
        created = await self.create_patient(full_name=full_name, phone=phone)
        return str(created.get("id"))

    async def create_individual_appointment(
        self,
        *,
        patient_id: str,
        practitioner_id: str,
        business_id: str,
        appointment_type_id: str,
        starts_at: datetime,
        ends_at: datetime,
        notes: str = "",
    ) -> dict[str, Any]:
        body = {
            "patient_id": patient_id,
            "practitioner_id": practitioner_id,
            "business_id": business_id,
            "appointment_type_id": appointment_type_id,
            "starts_at": to_cliniko_utc(starts_at),
            "ends_at": to_cliniko_utc(ends_at),
            "notes": notes,
        }
        return await self._request("POST", "/individual_appointments", json=body)

    async def cancel_individual_appointment(
        self, appointment_id: str, *, reason: str = "Cancelled via voice receptionist"
    ) -> dict[str, Any]:
        try:
            return await self._request(
                "PATCH",
                f"/individual_appointments/{appointment_id}",
                json={"cancelled_at": to_cliniko_utc(datetime.now(timezone.utc)), "cancellation_note": reason},
            )
        except ClinikoError:
            return await self._request("DELETE", f"/individual_appointments/{appointment_id}")

    async def list_all(self, resource: str) -> list[dict[str, Any]]:
        """Page through a collection resource (businesses, practitioners, ...)."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._request("GET", f"/{resource}", params={"per_page": 100, "page": page})
            items = data.get(resource) or []
            out.extend(items)
            links = data.get("links") or {}
            if not links.get("next") or not items:
                break
            page += 1
        return out

    async def create_business(self, name: str) -> dict[str, Any]:
        return await self._request("POST", "/businesses", json={"business_name": name})

    async def create_appointment_type(
        self,
        *,
        name: str,
        duration_minutes: int,
        color: str = "#B8D9FF",
        max_attendees: int = 1,
        business_ids: list[str] | None = None,
        practitioner_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        # Cliniko validates color + max_attendees; name/duration alone -> 422.
        body: dict[str, Any] = {
            "name": name,
            "duration_in_minutes": int(duration_minutes),
            "color": color,
            "max_attendees": int(max_attendees),
        }
        if business_ids:
            body["business_ids"] = business_ids
        if practitioner_ids:
            body["practitioner_ids"] = practitioner_ids
        return await self._request("POST", "/appointment_types", json=body)


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:  
        return resp.text
