"""
Start an outbound Bolna call (Bolna dials the recipient — no inbound DID needed).
POST https://api.bolna.ai/call
Authorization: Bearer BOLNA_API_KEY
Usage:
  python scripts/bolna_start_call.py --phone 9876543210 --name "Priya Sharma"
"""

from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bolna_client import bolna_client, load_dotenv

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Start Bolna outbound call")
    parser.add_argument("--phone", required=True, help="Recipient phone (10-digit Indian or E.164)")
    parser.add_argument("--name", default="", help="Caller name passed as user_data")
    parser.add_argument("--role", default="patient", help="Optional user_data.role")
    parser.add_argument("--agent-id", default=os.environ.get("BOLNA_AGENT_ID"), help="Bolna agent id")
    args = parser.parse_args()
    if not args.agent_id:
        print("Set BOLNA_AGENT_ID in .env or pass --agent-id", file=sys.stderr)
        return 1
    phone = args.phone.strip()
    if phone.startswith("+") is False and len(phone) == 10:
        phone = f"+91{phone}"
    body = {
        "agent_id": args.agent_id,
        "recipient_phone_number": phone,
        "user_data": {
            "name": args.name or "Caller",
            "role": args.role,
        },
    }

    with bolna_client() as client:
        resp = client.post("/call", json=body)
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400:
            print(resp.status_code, json.dumps(data, indent=2), file=sys.stderr)
            return 1
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"\nOutbound call started to {phone}")
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
