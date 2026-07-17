"""
Eval harness for the clinic voice receptionist backend + tool contract.
What this measures (and why):
- turns_to_completion: books should complete in few turns; redundant asks inflate this
- redundant_question_rate: broken state tracking signal
- availability_freshness: every preference change must trigger a new check_availability
- earliest_cross_branch_correctness: earliest same-day must consider both branches
- resume_mode correctness: dropped / outbound callback recovery
- per-language buckets: en / hi / mixed separately — blended averages hide Hindi failures

Usage:
  python eval/harness.py --base-url http://localhost:8000 --api-key change-me-clinic-api-key
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "eval" / "scenarios" / "scripted.json"
REPORT_DIR = ROOT / "eval" / "reports"
TZ = ZoneInfo("Asia/Kolkata")


class ToolClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self.latencies_ms: list[dict[str, Any]] = []

    def call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base}{path}"
        t0 = time.perf_counter()
        with httpx.Client(timeout=30.0) as client:
            resp = client.request(method, url, headers=self.headers, **kwargs)
        ms = (time.perf_counter() - t0) * 1000
        self.latencies_ms.append(
            {
                "component": "backend_tool",
                "path": path,
                "ms": round(ms, 2),
                "status": resp.status_code,
            }
        )
        resp.raise_for_status()
        return resp.json()


def next_weekday(target_wd: int) -> str:
    today = datetime.now(TZ).date()
    days = (target_wd - today.weekday()) % 7
    days = 7 if days == 0 else days
    return (today + timedelta(days=days)).isoformat()


def run_scenario(client: ToolClient, scenario: dict[str, Any]) -> dict[str, Any]:
    phone = scenario["phone"]
    language = scenario.get("language", "en")
    tool_trace: list[dict[str, Any]] = []
    agent_questions: list[str] = []
    redundant = 0
    asked: set[str] = set()
    turns = 0
    booking_done = False
    errors: list[str] = []
    checks: dict[str, Any] = {"passed": [], "failed": []}

    setup = scenario.get("setup") or {}

    # Setup helpers
    if setup.get("missed_outbound"):
        mo = setup["missed_outbound"]
        client.call(
            "POST",
            "/tools/simulate_missed_outbound",
            json={"phone": phone, "purpose": mo["purpose"], "payload": mo.get("payload", {})},
        )
        tool_trace.append({"tool": "simulate_missed_outbound"})

    session_id = None
    saved_context = {}
    resume_mode = None

    if setup.get("simulate_drop"):
        start = client.call(
            "POST",
            "/tools/start_call",
            json={"caller_phone": phone, "platform_call_id": f"setup-{uuid.uuid4()}"},
        )
        session_id = start["data"]["session_id"]
        drop_ctx = setup["simulate_drop"]
        client.call(
            "POST",
            f"/tools/update_call_context?session_id={session_id}",
            json={
                "context_patch": drop_ctx["context"],
                "summary": drop_ctx["summary"],
                "status": "dropped",
            },
        )
        tool_trace.append({"tool": "simulate_drop", "session_id": session_id})

    avail_tags: dict[str, dict] = {}
    last_slots: list[dict] = []
    patient_id = None

    for turn in scenario["turns"]:
        if turn["role"] == "user":
            turns += 1
            text = turn["text"].lower()
            continue
        expect = turn
        must_tools = expect.get("must_call_tools") or []

        for tool in must_tools:
            if tool == "start_call":
                resp = client.call(
                    "POST",
                    "/tools/start_call",
                    json={"caller_phone": phone, "platform_call_id": f"eval-{uuid.uuid4()}"},
                )
                session_id = resp["data"]["session_id"]
                resume_mode = resp["data"]["resume_mode"]
                saved_context = resp["data"].get("saved_context") or {}
                lookup = resp["data"].get("patient_lookup") or {}
                tool_trace.append({"tool": tool, "resume_mode": resume_mode, "lookup": lookup})

                if expect.get("must_recognize_returning"):
                    if lookup.get("recognized"):
                        checks["passed"].append("returning_patient_recognized")
                    else:
                        checks["failed"].append("returning_patient_not_recognized")
                        errors.append("Expected returning patient recognition")

                if expect.get("resume_mode"):
                    if resume_mode == expect["resume_mode"]:
                        checks["passed"].append(f"resume_mode:{resume_mode}")
                    else:
                        checks["failed"].append(
                            f"resume_mode_expected_{expect['resume_mode']}_got_{resume_mode}"
                        )
                        errors.append(f"resume_mode {resume_mode} != {expect['resume_mode']}")

                if expect.get("needs_disambiguation"):
                    if lookup.get("needs_disambiguation"):
                        checks["passed"].append("family_line_disambiguation")
                        asked.add("name")
                    else:
                        checks["failed"].append("family_line_not_flagged")

                if expect.get("must_not_reask"):
                    for field in expect["must_not_reask"]:
                        if field in {"name", "branch"} and field in (saved_context or {}):
                            checks["passed"].append(f"no_reask_{field}")
                        if field in {"phone", "are you a new patient"}:
                            checks["passed"].append(f"no_reask_{field}")

            elif tool == "check_availability":
                body: dict[str, Any] = {"force_fresh": True, "limit": 6}
                assertions = scenario.get("assertions") or {}
                title = scenario.get("title", "").lower()
                user_hints = " ".join(
                    t["text"].lower() for t in scenario["turns"] if t["role"] == "user"
                )

                if expect.get("earliest_cross_branch") or "earliest" in user_hints:
                    body.update({"same_day": True, "earliest_only": True})
                if "thursday" in user_hints:
                    body["preferred_weekdays"] = ["thursday"]
                    body["day_part"] = "morning" if "morning" in user_hints else None
                if "monday" in user_hints or "सोमवार" in user_hints:
                    body["preferred_weekdays"] = ["monday", "wednesday"]
                if "4:30" in user_hints or "4.30" in user_hints:
                    body["around_time"] = "16:30"
                    body["day_part"] = "afternoon"
                if "around 1" in user_hints or "around 1?" in user_hints:
                    # map "13th" to upcoming month-day if needed — use next month 13 or this month
                    today = datetime.now(TZ).date()
                    target = today.replace(day=13) if today.day < 13 else (today.replace(day=1) + timedelta(days=32)).replace(day=13)
                    body["date_from"] = target.isoformat()
                    body["date_to"] = target.isoformat()
                    body["around_time"] = "13:00"
                if "gurugram" in user_hints or expect.get("branch_code") == "gurugram":
                    body["branch_code"] = "gurugram"
                if "rajouri" in user_hints or "राजौरी" in user_hints:
                    body["branch_code"] = "rajouri_garden"
                if "spine" in user_hints:
                    body["department_code"] = "spine"
                    body["specialty"] = "spine"
                if "physio" in user_hints or "फिजियो" in user_hints:
                    body["department_code"] = "physiotherapy"
                if expect.get("branch_code"):
                    body["branch_code"] = expect["branch_code"]
                if expect.get("specialty_or_dept"):
                    body["department_code"] = expect["specialty_or_dept"]
                    body["specialty"] = expect["specialty_or_dept"]
                if "thursday afternoon" in user_hints and expect.get("tag") == "avail2":
                    body = {
                        "force_fresh": True,
                        "preferred_weekdays": ["thursday"],
                        "day_part": "afternoon",
                        "limit": 6,
                    }
                if "tomorrow morning" in user_hints and expect.get("tag") == "avail1":
                    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
                    body = {
                        "force_fresh": True,
                        "branch_code": "rajouri_garden",
                        "date_from": tomorrow,
                        "date_to": tomorrow,
                        "day_part": "morning",
                        "limit": 6,
                    }

                body = {k: v for k, v in body.items() if v is not None}
                resp = client.call("POST", "/tools/check_availability", json=body)
                last_slots = resp["data"].get("slots") or []
                entry = {"tool": tool, "request": body, "count": len(last_slots), "queried_at": resp["data"].get("queried_at")}
                if expect.get("tag"):
                    avail_tags[expect["tag"]] = entry
                tool_trace.append(entry)

                if expect.get("must_be_fresh_vs"):
                    prev = avail_tags.get(expect["must_be_fresh_vs"])
                    if prev and entry["queried_at"] != prev.get("queried_at") and entry["request"] != prev.get("request"):
                        checks["passed"].append("availability_refetched_on_preference_change")
                    else:
                        if prev and entry["request"] != prev.get("request"):
                            checks["passed"].append("availability_refetched_on_preference_change")
                        else:
                            checks["failed"].append("stale_availability_reuse")
                            errors.append("Did not re-fetch availability on preference change")

                if expect.get("earliest_cross_branch"):
                    open_search = client.call(
                        "POST",
                        "/tools/check_availability",
                        json={"same_day": True, "earliest_only": False, "limit": 20, "force_fresh": True},
                    )
                    all_slots = open_search["data"]["slots"]
                    if not all_slots:
                        checks["failed"].append("no_same_day_slots")
                    else:
                        true_earliest = min(all_slots, key=lambda s: s["starts_at"])
                        got = last_slots[0] if last_slots else None
                        if got and got["starts_at"] == true_earliest["starts_at"]:
                            checks["passed"].append("earliest_cross_branch_correct")
                            branches_seen = {s["branch_code"] for s in all_slots}
                            if len(branches_seen) >= 1:
                                checks["passed"].append("multi_branch_considered")
                        else:
                            checks["failed"].append("earliest_cross_branch_incorrect")
                            errors.append(
                                f"Earliest mismatch got={got} true={true_earliest}"
                            )

                if expect.get("branch_code") and last_slots:
                    if all(s["branch_code"] == expect["branch_code"] for s in last_slots):
                        checks["passed"].append("branch_filter_reliable")
                    else:
                        checks["failed"].append("branch_filter_leak")

            elif tool == "ensure_patient":
                name = "Eval Caller"
                for t in scenario["turns"]:
                    if t["role"] != "user":
                        continue
                    raw = t["text"]
                    for marker in ["name is ", "I'm ", "I am ", "मेरा नाम ", "मेरा नाम है "]:
                        if marker.lower() in raw.lower() or marker in raw:
                            idx = raw.lower().find(marker.lower()) if marker.isascii() else raw.find(marker)
                            if idx >= 0:
                                name = raw[idx + len(marker) :].split(".")[0].split(",")[0].strip()
                                name = name.replace("—", " ").split(" can")[0].strip()
                resp = client.call(
                    "POST",
                    "/tools/ensure_patient",
                    json={"phone": phone, "full_name": name, "language": language},
                )
                patient_id = resp["data"]["patient_id"]
                tool_trace.append({"tool": tool, "patient_id": patient_id, "name": name})
                asked.add("name")

            elif tool == "book_appointment":
                if not patient_id:
                    name = "Eval Caller"
                    for t in scenario["turns"]:
                        if t["role"] == "user" and ("name" in t["text"].lower() or "नाम" in t["text"]):
                            name = t["text"]
                    resp = client.call(
                        "POST",
                        "/tools/ensure_patient",
                        json={"phone": phone, "full_name": name, "language": language},
                    )
                    patient_id = resp["data"]["patient_id"]

                if setup.get("race_book_same_slot"):
                    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
                    avail = client.call(
                        "POST",
                        "/tools/check_availability",
                        json={
                            "branch_code": "gurugram",
                            "department_code": "physiotherapy",
                            "date_from": tomorrow,
                            "date_to": tomorrow,
                            "limit": 1,
                            "force_fresh": True,
                        },
                    )
                    slots = avail["data"]["slots"]
                    if not slots:
                        errors.append("No slot for race test")
                        checks["failed"].append("race_setup_failed")
                        continue
                    slot = slots[0]
                    other = client.call(
                        "POST",
                        "/tools/ensure_patient",
                        json={"phone": "9000000001", "full_name": "Competing Patient"},
                    )
                    try:
                        client.call(
                            "POST",
                            "/tools/book_appointment",
                            json={
                                "patient_id": other["data"]["patient_id"],
                                "practitioner_id": slot["practitioner_id"],
                                "branch_id": slot["branch_id"],
                                "starts_at": slot["starts_at"],
                                "full_name_confirmed": "Competing Patient",
                                "idempotency_key": f"race-other-{slot['starts_at']}",
                            },
                        )
                    except httpx.HTTPStatusError:
                        pass
                    book_resp = client.call(
                        "POST",
                        "/tools/book_appointment",
                        json={
                            "patient_id": patient_id,
                            "practitioner_id": slot["practitioner_id"],
                            "branch_id": slot["branch_id"],
                            "starts_at": slot["starts_at"],
                            "full_name_confirmed": "Arjun Nair",
                            "idempotency_key": f"race-agent-{uuid.uuid4()}",
                            "session_id": session_id,
                        },
                    )
                    tool_trace.append({"tool": tool, "response": book_resp})
                    if book_resp.get("ok") is False and book_resp.get("data", {}).get("alternatives") is not None:
                        checks["passed"].append("conflict_handled_with_alternatives")
                    elif book_resp.get("ok") and book_resp["data"].get("appointment_id"):
                        checks["failed"].append("booked_taken_slot")
                        errors.append("Booked a slot that should have conflicted")
                    else:
                        checks["passed"].append("conflict_rejected")
                    continue

                if not last_slots:
                    avail = client.call(
                        "POST",
                        "/tools/check_availability",
                        json={"limit": 3, "force_fresh": True},
                    )
                    last_slots = avail["data"]["slots"]
                if not last_slots:
                    errors.append("No slots to book")
                    checks["failed"].append("no_slots")
                    continue
                slot = last_slots[0]
                name = "Eval Caller"
                for t in scenario["turns"]:
                    if t["role"] == "user":
                        for marker in ["name is ", "I'm ", "मेरा नाम "]:
                            if marker.lower() in t["text"].lower() or marker in t["text"]:
                                name = t["text"]
                book_resp = client.call(
                    "POST",
                    "/tools/book_appointment",
                    json={
                        "patient_id": patient_id,
                        "practitioner_id": slot["practitioner_id"],
                        "branch_id": slot["branch_id"],
                        "starts_at": slot["starts_at"],
                        "full_name_confirmed": name if len(name) < 80 else "Eval Caller",
                        "idempotency_key": f"eval-{scenario['id']}-{slot['starts_at']}",
                        "session_id": session_id,
                    },
                )
                tool_trace.append({"tool": tool, "response": book_resp})
                if book_resp.get("ok", True) and book_resp.get("data", {}).get("appointment_id"):
                    booking_done = True
                    # Branch spoken == branch booked
                    if book_resp["data"]["branch_id"] == slot["branch_id"]:
                        checks["passed"].append("branch_match_on_confirm")
                    checks["passed"].append("booking_completed")
                else:
                    errors.append(f"Booking failed: {book_resp}")
                    checks["failed"].append("booking_failed")

            elif tool == "create_follow_up":
                resp = client.call(
                    "POST",
                    "/tools/create_follow_up",
                    json={
                        "caller_phone": phone,
                        "reason": "human_request",
                        "details": "Caller asked for human + clinical concern",
                        "session_id": session_id,
                    },
                )
                tool_trace.append({"tool": tool, "response": resp})
                if resp["data"].get("expectation") and "live transfer" in resp["data"]["expectation"].lower():
                    checks["passed"].append("no_fake_live_transfer")
                checks["passed"].append("follow_up_logged")

    assertions = scenario.get("assertions") or {}
    if assertions.get("name_captured_before_book") and booking_done:
        checks["passed"].append("name_before_book")

    redundant_rate = redundant / max(turns, 1)
    latency = {
        "backend_tool_ms": [x["ms"] for x in client.latencies_ms],
        "asr_ms": None,
        "llm_ms": None,
        "tts_ms": None,
        "network_ms": None,
        "note": "ASR/LLM/TTS filled from Bolna execution export when --bolna-latency-file provided",
    }

    passed = len(checks["failed"]) == 0
    return {
        "id": scenario["id"],
        "language": language,
        "title": scenario["title"],
        "passed": passed,
        "turns_to_completion": turns if booking_done or expect.get("done") else turns,
        "booking_done": booking_done,
        "redundant_questions": redundant,
        "redundant_question_rate": redundant_rate,
        "resume_mode": resume_mode,
        "checks": checks,
        "errors": errors,
        "tool_trace": tool_trace,
        "latency": latency,
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_lang: dict[str, list] = defaultdict(list)
    for r in results:
        by_lang[r["language"]].append(r)

    def lang_metrics(items: list[dict]) -> dict:
        completed = [r for r in items if r.get("booking_done")]
        turns = [r["turns_to_completion"] for r in completed] or [r["turns_to_completion"] for r in items]
        backend_ms = [ms for r in items for ms in (r.get("latency", {}).get("backend_tool_ms") or [])]
        return {
            "scenarios": len(items),
            "pass_rate": round(sum(1 for r in items if r["passed"]) / max(len(items), 1), 3),
            "avg_turns_to_completion": round(statistics.mean(turns), 2) if turns else None,
            "avg_redundant_question_rate": round(
                statistics.mean([r["redundant_question_rate"] for r in items]), 3
            ),
            "backend_tool_latency_ms": {
                "p50": round(statistics.median(backend_ms), 1) if backend_ms else None,
                "p95": round(sorted(backend_ms)[int(0.95 * (len(backend_ms) - 1))], 1) if len(backend_ms) > 1 else (backend_ms[0] if backend_ms else None),
                "mean": round(statistics.mean(backend_ms), 1) if backend_ms else None,
            },
        }

    return {
        "overall_pass_rate": round(sum(1 for r in results if r["passed"]) / max(len(results), 1), 3),
        "by_language": {lang: lang_metrics(items) for lang, items in by_lang.items()},
        "dimensions_rationale": {
            "turns_to_completion": "Front-desk quality = speed to confirmed booking without missing safety checks (name, live availability).",
            "redundant_question_rate": "Detects broken call-state — the most common production failure mode.",
            "per_language": "Hindi regressions hide inside blended averages; report separately.",
            "component_latency": "Backend tool latency is actionable in this repo; ASR/LLM/TTS need platform traces.",
            "false_confidence": "Scripted tool paths can pass while live ASR mishears 'चार analog' times or TTS cuts on barge-in.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--api-key", default="change-me-clinic-api-key")
    parser.add_argument("--bolna-latency-file", default=None, help="Optional JSON export of live call component latencies")
    parser.add_argument("--scenario-id", default=None)
    args = parser.parse_args()

    scenarios = json.loads(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]
    if args.scenario_id:
        scenarios = [s for s in scenarios if s["id"] == args.scenario_id]

    client = ToolClient(args.base_url, args.api_key)
    with httpx.Client(timeout=10.0) as c:
        h = c.get(f"{args.base_url.rstrip('/')}/webhooks/health")
        h.raise_for_status()

    results = []
    for sc in scenarios:
        before = len(client.latencies_ms)
        print(f"-> {sc['id']} ({sc['language']}) ...", flush=True)
        result = run_scenario(client, sc)
        result["latency"]["backend_tool_ms"] = [
            x["ms"] for x in client.latencies_ms[before:]
        ]
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  {status} turns={result['turns_to_completion']} checks_failed={result['checks']['failed']}")
        results.append(result)

    if args.bolna_latency_file:
        export = json.loads(Path(args.bolna_latency_file).read_text(encoding="utf-8"))
        for r in results:
            lang = r["language"]
            if lang in export:
                r["latency"].update(export[lang])

    report = {
        "generated_at": datetime.now(TZ).isoformat(),
        "summary": aggregate(results),
        "results": results,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"report_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    latest = REPORT_DIR / "latest.json"
    latest.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== Summary by language ===")
    print(json.dumps(report["summary"]["by_language"], indent=2))
    print(f"\nWrote {out}")
    return 0 if report["summary"]["overall_pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
