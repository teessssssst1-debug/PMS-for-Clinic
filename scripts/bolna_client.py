from __future__ import annotations
import os
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]  # project root


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

def bolna_headers() -> dict[str, str]:
    api_key = os.environ.get("BOLNA_API_KEY")
    if not api_key:
        raise SystemExit("Set BOLNA_API_KEY in .env")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

def bolna_base() -> str:
    return os.environ.get("BOLNA_API_BASE", "https://api.bolna.ai").rstrip("/")

def bolna_client() -> httpx.Client:
    return httpx.Client(
        base_url=bolna_base(),
        headers=bolna_headers(),
        timeout=60.0,
    )
