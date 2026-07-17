from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_db
from app.services.call_state import CallStateService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/bolna")
async def bolna_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """
    Bolna execution webhook — marks call completed/dropped for recovery.
    Whitelist Bolna IP 13.203.39.153 in production.
    """
    if settings.bolna_webhook_secret:
        secret = request.headers.get("X-Bolna-Secret") or request.headers.get("X-Webhook-Secret")
        if secret != settings.bolna_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    payload = await request.json()
    status = str(payload.get("status") or payload.get("call_status") or "").lower()
    meta = payload.get("metadata") or payload.get("context_details") or {}
    session_id = meta.get("session_id") or payload.get("session_id")
    if session_id:
        try:
            sid = UUID(session_id)
            if status in {"busy", "no-answer", "failed", "canceled", "cancelled"}:
                await CallStateService(db).update_context(sid, status="dropped")
            elif status in {"completed", "balanced", "call-completed"}:
                ctx_done = (meta.get("booking_completed") is True) or payload.get("booking_completed")
                await CallStateService(db).update_context(
                    sid, status="completed" if ctx_done else "dropped"
                )
        except ValueError:
            pass

    return {"ok": True}


@router.post("/bolna/call-disconnected")
async def bolna_disconnected(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
):
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    body = await request.json()
    session_id = body.get("session_id")
    if session_id:
        await CallStateService(db).mark_dropped(UUID(session_id))
    return {"ok": True}


@router.get("/health")
async def health():
    return {"status": "ok"}
