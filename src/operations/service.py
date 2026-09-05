"""Tenant-scoped persistence and deterministic business rules for inbound reception."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}" if digits else ""


def normalize_postal(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


class OperationalService:
    """Application service used by tools so the model never touches SQLite."""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._lock = Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS customers(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, first_name TEXT NOT NULL,
                      last_name TEXT, phone TEXT NOT NULL, email TEXT, address TEXT NOT NULL,
                      city TEXT NOT NULL, postal_code TEXT NOT NULL, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL, UNIQUE(organization_id,phone));
                    CREATE TABLE IF NOT EXISTS leads(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, customer_id TEXT,
                      source TEXT NOT NULL, caller_number TEXT, service_category TEXT,
                      service_hint TEXT, issue_description TEXT, urgency TEXT NOT NULL,
                      status TEXT NOT NULL, call_id TEXT NOT NULL, ai_summary TEXT,
                      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                      UNIQUE(organization_id,call_id));
                    CREATE TABLE IF NOT EXISTS offered_slots(
                      token TEXT PRIMARY KEY, organization_id TEXT NOT NULL, call_id TEXT NOT NULL,
                      technician_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                      service_code TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT);
                    CREATE TABLE IF NOT EXISTS jobs(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, customer_id TEXT NOT NULL,
                      lead_id TEXT NOT NULL, service_code TEXT NOT NULL, issue_description TEXT NOT NULL,
                      urgency TEXT NOT NULL, status TEXT NOT NULL, safety_notes TEXT, access_notes TEXT,
                      technician_brief TEXT, call_id TEXT NOT NULL, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS appointments(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, job_id TEXT NOT NULL,
                      customer_id TEXT NOT NULL, technician_id TEXT NOT NULL, starts_at TEXT NOT NULL,
                      ends_at TEXT NOT NULL, timezone TEXT NOT NULL, status TEXT NOT NULL,
                      confirmation_ref TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS idx_appt_time ON appointments(
                      organization_id,technician_id,starts_at,ends_at,status);
                    CREATE TABLE IF NOT EXISTS escalations(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, call_id TEXT NOT NULL,
                      lead_id TEXT, kind TEXT NOT NULL, reason TEXT NOT NULL, priority TEXT NOT NULL,
                      status TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS messages(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, appointment_id TEXT,
                      provider TEXT NOT NULL, recipient TEXT NOT NULL, provider_message_id TEXT,
                      status TEXT NOT NULL, failure_reason TEXT, created_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS call_artifacts(
                      call_id TEXT NOT NULL, organization_id TEXT NOT NULL, transcript_json TEXT,
                      verified_facts_json TEXT NOT NULL, ai_summary TEXT, technician_brief TEXT,
                      detected_languages_json TEXT NOT NULL, outcome TEXT NOT NULL,
                      updated_at TEXT NOT NULL, PRIMARY KEY(organization_id,call_id));
                    CREATE TABLE IF NOT EXISTS audit_events(
                      id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL,
                      call_id TEXT NOT NULL, event_type TEXT NOT NULL, entity_type TEXT,
                      entity_id TEXT, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
                    """
                )
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
            self._initialized = True

    def _audit(self, db: sqlite3.Connection, org: str, call_id: str, event: str,
               entity: str = "", entity_id: str = "", detail: Optional[Dict[str, Any]] = None) -> None:
        db.execute(
            "INSERT INTO audit_events(organization_id,call_id,event_type,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (org, call_id, event, entity, entity_id, _json(detail or {}), _now()),
        )

    async def audit(self, org: str, call_id: str, event: str, detail: Dict[str, Any]) -> None:
        await self.initialize()
        def write() -> None:
            with self._connect() as db:
                self._audit(db, org, call_id, event, detail=detail)
        await asyncio.to_thread(write)

    async def find_customer(self, org: str, phone: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        def query() -> Optional[Dict[str, Any]]:
            with self._connect() as db:
                row = db.execute(
                    "SELECT id,first_name,last_name,phone,email,address,city,postal_code FROM customers WHERE organization_id=? AND phone=?",
                    (org, normalize_phone(phone)),
                ).fetchone()
                return dict(row) if row else None
        return await asyncio.to_thread(query)

    async def upsert_customer(self, org: str, call_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            phone = normalize_phone(data.get("phone", ""))
            required = {key: str(data.get(key) or "").strip() for key in ("first_name", "address", "city", "postal_code")}
            if not phone or any(not value for value in required.values()):
                raise ValueError("first_name, phone, address, city, and postal_code are required")
            now = _now()
            with self._connect() as db:
                row = db.execute("SELECT id FROM customers WHERE organization_id=? AND phone=?", (org, phone)).fetchone()
                customer_id = row["id"] if row else f"cus_{secrets.token_hex(8)}"
                values = (required["first_name"], str(data.get("last_name") or "").strip(),
                          str(data.get("email") or "").strip(), required["address"], required["city"],
                          normalize_postal(required["postal_code"]), now, customer_id, org)
                if row:
                    db.execute("UPDATE customers SET first_name=?,last_name=?,email=?,address=?,city=?,postal_code=?,updated_at=? WHERE id=? AND organization_id=?", values)
                    created = False
                else:
                    db.execute("INSERT INTO customers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (customer_id, org, required["first_name"], str(data.get("last_name") or "").strip(), phone,
                                str(data.get("email") or "").strip(), required["address"], required["city"],
                                normalize_postal(required["postal_code"]), now, now))
                    created = True
                self._audit(db, org, call_id, "customer_created" if created else "customer_updated", "customer", customer_id)
            return {"customer_id": customer_id, "created": created}
        return await asyncio.to_thread(write)

    async def upsert_lead(self, org: str, call_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            now = _now()
            with self._connect() as db:
                row = db.execute("SELECT id FROM leads WHERE organization_id=? AND call_id=?", (org, call_id)).fetchone()
                lead_id = row["id"] if row else f"lead_{secrets.token_hex(8)}"
                common = (data.get("customer_id"), "phone", normalize_phone(data.get("caller_number", "")),
                          str(data.get("service_category") or "other"), str(data.get("service_hint") or ""),
                          str(data.get("issue_description") or ""), str(data.get("urgency") or "standard"),
                          str(data.get("status") or "open"), str(data.get("ai_summary") or ""), now)
                if row:
                    db.execute("UPDATE leads SET customer_id=COALESCE(?,customer_id),source=?,caller_number=?,service_category=?,service_hint=?,issue_description=?,urgency=?,status=?,ai_summary=?,updated_at=? WHERE id=? AND organization_id=?", common + (lead_id, org))
                    created = False
                else:
                    db.execute(
                        "INSERT INTO leads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (lead_id, org, data.get("customer_id"), "phone",
                         normalize_phone(data.get("caller_number", "")),
                         str(data.get("service_category") or "other"),
                         str(data.get("service_hint") or ""),
                         str(data.get("issue_description") or ""),
                         str(data.get("urgency") or "standard"),
                         str(data.get("status") or "open"), call_id,
                         str(data.get("ai_summary") or ""), now, now),
                    )
                    created = True
                self._audit(db, org, call_id, "lead_created" if created else "lead_updated", "lead", lead_id)
            return {"lead_id": lead_id, "created": created}
        return await asyncio.to_thread(write)

    async def offer_slots(self, org: str, call_id: str, service_code: str, start_date: str,
                          days: int, config: Dict[str, Any]) -> list[Dict[str, Any]]:
        await self.initialize()
        def query() -> list[Dict[str, Any]]:
            scheduling = config.get("scheduling") or {}
            if not scheduling.get("enabled"):
                raise RuntimeError("Scheduling is not configured")
            catalog = config.get("service_catalog") or {}
            if service_code not in catalog or not catalog[service_code].get("enabled", True):
                raise ValueError("Service is not in the configured catalog")
            tz_name = str(config.get("timezone") or "America/Vancouver")
            tz = ZoneInfo(tz_name)
            try:
                first_day = datetime.fromisoformat(start_date).date()
            except ValueError as exc:
                raise ValueError("start_date must be YYYY-MM-DD") from exc
            duration = int(catalog[service_code].get("duration_minutes", 120))
            notice = int(scheduling.get("minimum_notice_minutes", 120))
            interval = int(scheduling.get("slot_interval_minutes", 60))
            limit = min(max(int(scheduling.get("max_offered_slots", 3)), 1), 6)
            hours = scheduling.get("business_hours") or {}
            technicians = scheduling.get("technicians") or []
            if not hours or not technicians:
                raise RuntimeError("Business hours and technician capacity must be configured")
            current = datetime.now(tz)
            result: list[Dict[str, Any]] = []
            with self._connect() as db:
                for offset in range(max(1, min(days, 21))):
                    day = first_day + timedelta(days=offset)
                    window = hours.get(day.strftime("%a").lower())
                    if not window:
                        continue
                    for tech in technicians:
                        if service_code not in set(tech.get("skills") or []) and "*" not in set(tech.get("skills") or []):
                            continue
                        sh, sm = map(int, str(window[0]).split(":")); eh, em = map(int, str(window[1]).split(":"))
                        candidate = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
                        closing = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
                        while candidate + timedelta(minutes=duration) <= closing:
                            ending = candidate + timedelta(minutes=duration)
                            clash = db.execute("SELECT 1 FROM appointments WHERE organization_id=? AND technician_id=? AND status='confirmed' AND starts_at<? AND ends_at>? LIMIT 1", (org, str(tech["id"]), ending.isoformat(), candidate.isoformat())).fetchone()
                            if candidate >= current + timedelta(minutes=notice) and not clash:
                                token = secrets.token_urlsafe(18)
                                expires = (current + timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
                                db.execute("INSERT INTO offered_slots VALUES(?,?,?,?,?,?,?,?,NULL)",
                                           (token, org, call_id, str(tech["id"]), candidate.isoformat(), ending.isoformat(), service_code, expires))
                                result.append({"slot_token": token, "starts_at": candidate.isoformat(), "ends_at": ending.isoformat(), "timezone": tz_name})
                                if len(result) >= limit:
                                    self._audit(db, org, call_id, "availability_retrieved", detail={"service_code": service_code, "slot_count": len(result)})
                                    return result
                            candidate += timedelta(minutes=interval)
                self._audit(db, org, call_id, "availability_retrieved", detail={"service_code": service_code, "slot_count": len(result)})
            return result
        return await asyncio.to_thread(query)

    async def book(self, org: str, call_id: str, slot_token: str, customer_id: str,
                   lead_id: str, data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            now = _now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                customer = db.execute("SELECT * FROM customers WHERE id=? AND organization_id=?", (customer_id, org)).fetchone()
                lead = db.execute("SELECT * FROM leads WHERE id=? AND organization_id=? AND call_id=?", (lead_id, org, call_id)).fetchone()
                slot = db.execute("SELECT * FROM offered_slots WHERE token=? AND organization_id=? AND call_id=? AND consumed_at IS NULL", (slot_token, org, call_id)).fetchone()
                if not customer or not lead or not slot:
                    raise ValueError("Customer, lead, or offered slot is invalid for this call")
                if datetime.fromisoformat(slot["expires_at"]) <= datetime.now(timezone.utc):
                    raise RuntimeError("The offered slot expired; check availability again")
                clash = db.execute("SELECT 1 FROM appointments WHERE organization_id=? AND technician_id=? AND status='confirmed' AND starts_at<? AND ends_at>? LIMIT 1", (org, slot["technician_id"], slot["ends_at"], slot["starts_at"])).fetchone()
                if clash:
                    raise RuntimeError("The selected slot is no longer available")
                job_id, appointment_id = f"job_{secrets.token_hex(8)}", f"appt_{secrets.token_hex(8)}"
                confirmation = f"CCS-{secrets.token_hex(3).upper()}"
                issue = str(data.get("issue_description") or lead["issue_description"])
                urgency = str(data.get("urgency") or lead["urgency"])
                brief = "\n".join(filter(None, [
                    f"Customer: {customer['first_name']} {customer['last_name'] or ''}".strip(),
                    f"Phone: {customer['phone']}", f"Address: {customer['address']}, {customer['city']} {customer['postal_code']}",
                    f"Service: {slot['service_code']}", f"Issue: {issue}", f"Urgency: {urgency}",
                    f"Appointment: {slot['starts_at']} to {slot['ends_at']}",
                    f"Safety: {data.get('safety_notes')}" if data.get("safety_notes") else "",
                    f"Access: {data.get('access_notes')}" if data.get("access_notes") else "",
                ]))
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (job_id, org, customer_id, lead_id, slot["service_code"], issue, urgency, "scheduled",
                            str(data.get("safety_notes") or ""), str(data.get("access_notes") or ""), brief, call_id, now, now))
                db.execute("INSERT INTO appointments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                           (appointment_id, org, job_id, customer_id, slot["technician_id"], slot["starts_at"], slot["ends_at"],
                            str(config.get("timezone") or "America/Vancouver"), "confirmed", confirmation, now, now))
                db.execute("UPDATE offered_slots SET consumed_at=? WHERE token=?", (now, slot_token))
                db.execute("UPDATE leads SET status='booked',customer_id=?,updated_at=? WHERE id=? AND organization_id=?", (customer_id, now, lead_id, org))
                self._audit(db, org, call_id, "booking_attempted", "appointment", appointment_id)
                self._audit(db, org, call_id, "booking_succeeded", "appointment", appointment_id, {"job_id": job_id})
                self._audit(db, org, call_id, "technician_brief_created", "job", job_id)
            return {"appointment_id": appointment_id, "job_id": job_id, "confirmation_ref": confirmation,
                    "starts_at": slot["starts_at"], "ends_at": slot["ends_at"], "technician_brief": brief,
                    "customer_phone": customer["phone"], "customer_address": f"{customer['address']}, {customer['city']} {customer['postal_code']}"}
        return await asyncio.to_thread(write)

    async def create_escalation(self, org: str, call_id: str, lead_id: str, kind: str,
                                reason: str, priority: str) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            escalation_id = f"esc_{secrets.token_hex(8)}"
            with self._connect() as db:
                if lead_id and not db.execute("SELECT 1 FROM leads WHERE id=? AND organization_id=?", (lead_id, org)).fetchone():
                    raise ValueError("Lead is not valid for this organization")
                db.execute("INSERT INTO escalations VALUES(?,?,?,?,?,?,?,?,?)", (escalation_id, org, call_id, lead_id or None, kind, reason, priority, "open", _now()))
                self._audit(db, org, call_id, "human_handoff_requested", "escalation", escalation_id, {"kind": kind, "priority": priority})
            return {"escalation_id": escalation_id, "status": "open"}
        return await asyncio.to_thread(write)

    async def find_appointments(self, org: str, phone: str) -> list[Dict[str, Any]]:
        await self.initialize()
        def query() -> list[Dict[str, Any]]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT a.id,a.job_id,a.starts_at,a.ends_at,a.timezone,a.status,a.confirmation_ref,j.service_code "
                    "FROM appointments a JOIN customers c ON c.id=a.customer_id AND c.organization_id=a.organization_id "
                    "JOIN jobs j ON j.id=a.job_id AND j.organization_id=a.organization_id "
                    "WHERE a.organization_id=? AND c.phone=? AND a.status='confirmed' AND a.ends_at>=? ORDER BY a.starts_at LIMIT 5",
                    (org, normalize_phone(phone), _now()),
                ).fetchall()
                return [dict(row) for row in rows]
        return await asyncio.to_thread(query)

    async def change_appointment(self, org: str, call_id: str, appointment_id: str,
                                 action: str, slot_token: str = "") -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            now = _now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                appointment = db.execute(
                    "SELECT * FROM appointments WHERE id=? AND organization_id=? AND status='confirmed'",
                    (appointment_id, org),
                ).fetchone()
                if not appointment:
                    raise ValueError("Confirmed appointment not found for this organization")
                if action == "cancel":
                    db.execute("UPDATE appointments SET status='cancelled',updated_at=? WHERE id=? AND organization_id=?",
                               (now, appointment_id, org))
                    db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND organization_id=?",
                               (now, appointment["job_id"], org))
                    self._audit(db, org, call_id, "appointment_cancelled", "appointment", appointment_id)
                    return {"appointment_id": appointment_id, "status": "cancelled"}
                if action != "reschedule":
                    raise ValueError("action must be cancel or reschedule")
                slot = db.execute(
                    "SELECT * FROM offered_slots WHERE token=? AND organization_id=? AND call_id=? AND consumed_at IS NULL",
                    (slot_token, org, call_id),
                ).fetchone()
                if not slot or datetime.fromisoformat(slot["expires_at"]) <= datetime.now(timezone.utc):
                    raise RuntimeError("The replacement slot is invalid or expired")
                clash = db.execute(
                    "SELECT 1 FROM appointments WHERE organization_id=? AND technician_id=? AND id<>? AND status='confirmed' AND starts_at<? AND ends_at>? LIMIT 1",
                    (org, slot["technician_id"], appointment_id, slot["ends_at"], slot["starts_at"]),
                ).fetchone()
                if clash:
                    raise RuntimeError("The replacement slot is no longer available")
                db.execute(
                    "UPDATE appointments SET technician_id=?,starts_at=?,ends_at=?,updated_at=? WHERE id=? AND organization_id=?",
                    (slot["technician_id"], slot["starts_at"], slot["ends_at"], now, appointment_id, org),
                )
                db.execute("UPDATE offered_slots SET consumed_at=? WHERE token=?", (now, slot_token))
                self._audit(db, org, call_id, "appointment_rescheduled", "appointment", appointment_id)
                return {"appointment_id": appointment_id, "status": "confirmed",
                        "starts_at": slot["starts_at"], "ends_at": slot["ends_at"]}
        return await asyncio.to_thread(write)

    async def save_artifacts(self, org: str, call_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            with self._connect() as db:
                db.execute("INSERT INTO call_artifacts VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(organization_id,call_id) DO UPDATE SET transcript_json=excluded.transcript_json,verified_facts_json=excluded.verified_facts_json,ai_summary=excluded.ai_summary,technician_brief=excluded.technician_brief,detected_languages_json=excluded.detected_languages_json,outcome=excluded.outcome,updated_at=excluded.updated_at",
                           (call_id, org, _json(data.get("transcript") or []), _json(data.get("verified_facts") or {}),
                            str(data.get("ai_summary") or ""), str(data.get("technician_brief") or ""),
                            _json(data.get("languages") or []), str(data.get("outcome") or "unknown"), _now()))
                self._audit(db, org, call_id, "call_summary_generated", "call", call_id, {"outcome": data.get("outcome")})
                self._audit(db, org, call_id, "call_completed", "call", call_id, {"outcome": data.get("outcome")})
            return {"call_id": call_id, "saved": True}
        return await asyncio.to_thread(write)

    async def send_sms(self, org: str, appointment_id: str, recipient: str, body: str,
                       config: Dict[str, Any]) -> Dict[str, Any]:
        await self.initialize()
        sms = config.get("sms") or {}
        provider = str(sms.get("provider") or "twilio")
        sid = os.getenv(str(sms.get("account_sid_env") or "TWILIO_ACCOUNT_SID"), "").strip()
        token = os.getenv(str(sms.get("auth_token_env") or "TWILIO_AUTH_TOKEN"), "").strip()
        sender = os.getenv(str(sms.get("from_number_env") or "TWILIO_SMS_FROM"), "").strip()
        status, reason, provider_id = "not_configured", "SMS provider is not configured", ""
        if sms.get("enabled") and provider == "twilio" and all((sid, token, sender)):
            try:
                url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
                async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(sid, token), timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.post(url, data={"To": normalize_phone(recipient), "From": sender, "Body": body}) as response:
                        payload = await response.json(content_type=None)
                        if response.status < 300:
                            status, reason, provider_id = str(payload.get("status") or "queued"), "", str(payload.get("sid") or "")
                        else:
                            status, reason = "failed", str(payload.get("message") or f"HTTP {response.status}")[:300]
            except Exception as exc:
                logger.warning("Confirmation SMS failed", appointment_id=appointment_id, error_type=type(exc).__name__)
                status, reason = "failed", type(exc).__name__
        def record() -> Dict[str, Any]:
            message_id = f"msg_{secrets.token_hex(8)}"
            with self._connect() as db:
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)", (message_id, org, appointment_id or None, provider,
                           normalize_phone(recipient), provider_id, status, reason, _now()))
                call_row = db.execute(
                    "SELECT j.call_id FROM appointments a JOIN jobs j ON j.id=a.job_id AND j.organization_id=a.organization_id "
                    "WHERE a.id=? AND a.organization_id=?",
                    (appointment_id, org),
                ).fetchone()
                if call_row:
                    self._audit(db, org, call_row["call_id"],
                                "sms_sent" if status not in {"failed", "not_configured"} else "sms_failed",
                                "message", message_id, {"provider": provider, "status": status})
            return {"message_id": message_id, "provider": provider, "send_status": status,
                    "sent": status not in {"failed", "not_configured"}, "failure_reason": reason}
        return await asyncio.to_thread(record)


_SERVICES: Dict[str, OperationalService] = {}
_SERVICES_LOCK = Lock()


def get_operational_service(db_path: str) -> OperationalService:
    resolved = str(Path(db_path).resolve())
    with _SERVICES_LOCK:
        if resolved not in _SERVICES:
            _SERVICES[resolved] = OperationalService(resolved)
        return _SERVICES[resolved]
