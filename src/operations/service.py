"""Tenant-scoped persistence and deterministic business rules for inbound reception."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
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


ACTIVE_APPOINTMENT_STATUSES = ("confirmed", "booked")
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _aware_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed


def _clock(value: str) -> time:
    try:
        parsed = time.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Working-hour values must use HH:MM") from exc
    if parsed.tzinfo is not None:
        raise ValueError("Working-hour values must be local HH:MM values")
    return parsed.replace(second=0, microsecond=0)


def _daily_windows(raw: Any) -> list[tuple[time, time]]:
    if not raw:
        return []
    values = raw
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and all(isinstance(item, str) for item in raw):
        values = [raw]
    if not isinstance(values, (list, tuple)):
        raise ValueError("Schedule day must contain a time pair or a list of time pairs")
    result: list[tuple[time, time]] = []
    for value in values:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("Each working interval must contain start and end")
        starts, ends = _clock(value[0]), _clock(value[1])
        if starts >= ends:
            raise ValueError("Working interval end must be after start")
        result.append((starts, ends))
    result.sort(key=lambda item: item[0])
    for previous, current in zip(result, result[1:]):
        if current[0] < previous[1]:
            raise ValueError("Working intervals must not overlap")
    return result


def _overlaps(starts: datetime, ends: datetime, other_start: datetime, other_end: datetime) -> bool:
    return starts < other_end and ends > other_start


def _contained_in_local_windows(starts: datetime, ends: datetime, tz_name: str,
                                windows: list[tuple[time, time]]) -> bool:
    tz = ZoneInfo(tz_name)
    local_start, local_end = starts.astimezone(tz), ends.astimezone(tz)
    if local_start.date() != local_end.date():
        return False
    return any(local_start.time().replace(tzinfo=None) >= window_start and
               local_end.time().replace(tzinfo=None) <= window_end
               for window_start, window_end in windows)


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
                    CREATE TABLE IF NOT EXISTS technicians(
                      id TEXT NOT NULL, organization_id TEXT NOT NULL, display_name TEXT NOT NULL,
                      active INTEGER NOT NULL DEFAULT 1, timezone TEXT NOT NULL,
                      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                      PRIMARY KEY(organization_id,id));
                    CREATE TABLE IF NOT EXISTS technician_services(
                      organization_id TEXT NOT NULL, technician_id TEXT NOT NULL,
                      service_code TEXT NOT NULL,
                      PRIMARY KEY(organization_id,technician_id,service_code),
                      FOREIGN KEY(organization_id,technician_id)
                        REFERENCES technicians(organization_id,id) ON DELETE CASCADE);
                    CREATE TABLE IF NOT EXISTS technician_working_hours(
                      id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id TEXT NOT NULL,
                      technician_id TEXT NOT NULL, weekday INTEGER NOT NULL,
                      starts_local TEXT NOT NULL, ends_local TEXT NOT NULL,
                      FOREIGN KEY(organization_id,technician_id)
                        REFERENCES technicians(organization_id,id) ON DELETE CASCADE,
                      UNIQUE(organization_id,technician_id,weekday,starts_local,ends_local));
                    CREATE TABLE IF NOT EXISTS technician_time_off(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                      technician_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                      reason TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      FOREIGN KEY(organization_id,technician_id)
                        REFERENCES technicians(organization_id,id) ON DELETE CASCADE);
                    CREATE TABLE IF NOT EXISTS technician_schedule_blocks(
                      id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
                      technician_id TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                      reason TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      FOREIGN KEY(organization_id,technician_id)
                        REFERENCES technicians(organization_id,id) ON DELETE CASCADE);
                    CREATE INDEX IF NOT EXISTS idx_technician_time_off
                      ON technician_time_off(organization_id,technician_id,status,starts_at,ends_at);
                    CREATE INDEX IF NOT EXISTS idx_technician_blocks
                      ON technician_schedule_blocks(organization_id,technician_id,status,starts_at,ends_at);
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

    @staticmethod
    def _technician_record(db: sqlite3.Connection, org: str, technician_id: str) -> Optional[Dict[str, Any]]:
        row = db.execute(
            "SELECT id,organization_id,display_name,active,timezone,created_at,updated_at "
            "FROM technicians WHERE organization_id=? AND id=?",
            (org, technician_id),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["active"] = bool(result["active"])
        result["service_ids"] = [item[0] for item in db.execute(
            "SELECT service_code FROM technician_services WHERE organization_id=? AND technician_id=? "
            "ORDER BY service_code", (org, technician_id)).fetchall()]
        hours: Dict[str, list[list[str]]] = {}
        for item in db.execute(
            "SELECT weekday,starts_local,ends_local FROM technician_working_hours "
            "WHERE organization_id=? AND technician_id=? ORDER BY weekday,starts_local",
            (org, technician_id),
        ).fetchall():
            hours.setdefault(WEEKDAY_KEYS[item["weekday"]], []).append(
                [item["starts_local"], item["ends_local"]])
        result["working_hours"] = hours
        result["time_off"] = [dict(item) for item in db.execute(
            "SELECT id,starts_at,ends_at,reason,status,created_at,updated_at FROM technician_time_off "
            "WHERE organization_id=? AND technician_id=? ORDER BY starts_at", (org, technician_id)).fetchall()]
        result["schedule_blocks"] = [dict(item) for item in db.execute(
            "SELECT id,starts_at,ends_at,reason,status,created_at,updated_at FROM technician_schedule_blocks "
            "WHERE organization_id=? AND technician_id=? ORDER BY starts_at", (org, technician_id)).fetchall()]
        return result

    async def upsert_technician(self, org: str, data: Dict[str, Any],
                                service_catalog: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Atomically create/update one tenant-scoped technician and its recurring capacity."""
        await self.initialize()
        def write() -> Dict[str, Any]:
            technician_id = str(data.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", technician_id):
                raise ValueError("Technician id must be a stable 1-64 character identifier")
            tz_name = str(data.get("timezone") or "").strip()
            try:
                ZoneInfo(tz_name)
            except Exception as exc:
                raise ValueError("Technician timezone is invalid") from exc
            services = sorted({str(item).strip() for item in data.get("service_ids") or [] if str(item).strip()})
            if service_catalog is not None:
                invalid = [item for item in services if item not in service_catalog or
                           not isinstance(service_catalog[item], dict) or
                           not service_catalog[item].get("enabled", True)]
                if invalid:
                    raise ValueError(f"Unknown or disabled service ids: {', '.join(invalid)}")
            schedule = data.get("working_hours") or {}
            if not isinstance(schedule, dict):
                raise ValueError("working_hours must be keyed by weekday")
            normalized_hours: list[tuple[int, str, str]] = []
            for key, raw_windows in schedule.items():
                normalized_key = str(key).strip().lower()[:3]
                if normalized_key not in WEEKDAY_KEYS:
                    raise ValueError(f"Unknown weekday: {key}")
                for starts, ends in _daily_windows(raw_windows):
                    normalized_hours.append((WEEKDAY_KEYS.index(normalized_key),
                                             starts.strftime("%H:%M"), ends.strftime("%H:%M")))
            now = _now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute(
                    "SELECT created_at FROM technicians WHERE organization_id=? AND id=?",
                    (org, technician_id),
                ).fetchone()
                db.execute(
                    "INSERT INTO technicians(id,organization_id,display_name,active,timezone,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(organization_id,id) DO UPDATE SET "
                    "display_name=excluded.display_name,active=excluded.active,timezone=excluded.timezone,updated_at=excluded.updated_at",
                    (technician_id, org, str(data.get("display_name") or technician_id).strip() or technician_id,
                     1 if data.get("active", True) else 0, tz_name, existing["created_at"] if existing else now, now),
                )
                db.execute("DELETE FROM technician_services WHERE organization_id=? AND technician_id=?", (org, technician_id))
                db.executemany(
                    "INSERT INTO technician_services(organization_id,technician_id,service_code) VALUES(?,?,?)",
                    [(org, technician_id, item) for item in services],
                )
                db.execute("DELETE FROM technician_working_hours WHERE organization_id=? AND technician_id=?", (org, technician_id))
                db.executemany(
                    "INSERT INTO technician_working_hours(organization_id,technician_id,weekday,starts_local,ends_local) "
                    "VALUES(?,?,?,?,?)",
                    [(org, technician_id, weekday, starts, ends) for weekday, starts, ends in normalized_hours],
                )
                self._audit(db, org, "admin", "technician_updated" if existing else "technician_created",
                            "technician", technician_id)
                return self._technician_record(db, org, technician_id) or {}
        return await asyncio.to_thread(write)

    async def list_technicians(self, org: str) -> list[Dict[str, Any]]:
        await self.initialize()
        def query() -> list[Dict[str, Any]]:
            with self._connect() as db:
                ids = [row[0] for row in db.execute(
                    "SELECT id FROM technicians WHERE organization_id=? ORDER BY display_name,id", (org,)).fetchall()]
                return [record for item in ids if (record := self._technician_record(db, org, item))]
        return await asyncio.to_thread(query)

    async def get_technician(self, org: str, technician_id: str) -> Optional[Dict[str, Any]]:
        await self.initialize()
        def query() -> Optional[Dict[str, Any]]:
            with self._connect() as db:
                return self._technician_record(db, org, technician_id)
        return await asyncio.to_thread(query)

    async def set_technician_exception(self, org: str, technician_id: str, kind: str,
                                       data: Dict[str, Any]) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            table = {"time_off": "technician_time_off", "block": "technician_schedule_blocks"}.get(kind)
            if not table:
                raise ValueError("Exception kind must be time_off or block")
            starts = _aware_datetime(str(data.get("start_datetime") or ""), "start_datetime")
            ends = _aware_datetime(str(data.get("end_datetime") or ""), "end_datetime")
            if starts >= ends:
                raise ValueError("end_datetime must be after start_datetime")
            entry_id = str(data.get("id") or f"{kind}_{secrets.token_hex(8)}")
            now = _now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM technicians WHERE organization_id=? AND id=?",
                                  (org, technician_id)).fetchone():
                    raise ValueError("Technician not found for this organization")
                db.execute(
                    f"INSERT INTO {table}(id,organization_id,technician_id,starts_at,ends_at,reason,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (entry_id, org, technician_id, starts.isoformat(), ends.isoformat(),
                     str(data.get("reason") or "").strip(), "active", now, now),
                )
                self._audit(db, org, "admin", f"technician_{kind}_created", kind, entry_id,
                            {"technician_id": technician_id})
            return {"id": entry_id, "technician_id": technician_id, "start_datetime": starts.isoformat(),
                    "end_datetime": ends.isoformat(), "reason": str(data.get("reason") or "").strip(),
                    "status": "active"}
        return await asyncio.to_thread(write)

    async def cancel_technician_exception(self, org: str, technician_id: str,
                                          kind: str, entry_id: str) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            table = {"time_off": "technician_time_off", "block": "technician_schedule_blocks"}.get(kind)
            if not table:
                raise ValueError("Exception kind must be time_off or block")
            with self._connect() as db:
                cursor = db.execute(
                    f"UPDATE {table} SET status='cancelled',updated_at=? "
                    "WHERE id=? AND organization_id=? AND technician_id=? AND status='active'",
                    (_now(), entry_id, org, technician_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Active schedule exception not found for this organization")
                self._audit(db, org, "admin", f"technician_{kind}_cancelled", kind, entry_id,
                            {"technician_id": technician_id})
            return {"id": entry_id, "status": "cancelled"}
        return await asyncio.to_thread(write)

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

    @staticmethod
    def _buffer_minutes(config: Dict[str, Any]) -> tuple[int, int]:
        scheduling = config.get("scheduling") or {}
        try:
            before = int(scheduling.get("travel_buffer_before_minutes", 0)) + int(
                scheduling.get("preparation_buffer_minutes", 0))
            after = int(scheduling.get("travel_buffer_after_minutes", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("Scheduling buffers must be whole minutes") from exc
        if before < 0 or after < 0:
            raise ValueError("Scheduling buffers cannot be negative")
        return before, after

    def _slot_block_reason(self, db: sqlite3.Connection, org: str, technician: sqlite3.Row,
                           service_code: str, starts: datetime, ends: datetime,
                           config: Dict[str, Any], exclude_appointment_id: str = "") -> Optional[str]:
        if not bool(technician["active"]):
            return "inactive_technician"
        eligible = db.execute(
            "SELECT 1 FROM technician_services WHERE organization_id=? AND technician_id=? AND service_code=?",
            (org, technician["id"], service_code),
        ).fetchone()
        if not eligible:
            return "service_ineligible"
        before, after = self._buffer_minutes(config)
        occupied_start = starts - timedelta(minutes=before)
        occupied_end = ends + timedelta(minutes=after)

        org_tz_name = str(config.get("timezone") or "America/Vancouver")
        local_org_start = occupied_start.astimezone(ZoneInfo(org_tz_name))
        business_raw = (config.get("scheduling") or {}).get("business_hours") or {}
        business_windows = _daily_windows(business_raw.get(WEEKDAY_KEYS[local_org_start.weekday()]))
        if not business_windows or not _contained_in_local_windows(
                occupied_start, occupied_end, org_tz_name, business_windows):
            return "outside_business_hours"

        local_tech_start = occupied_start.astimezone(ZoneInfo(technician["timezone"]))
        hour_rows = db.execute(
            "SELECT starts_local,ends_local FROM technician_working_hours "
            "WHERE organization_id=? AND technician_id=? AND weekday=? ORDER BY starts_local",
            (org, technician["id"], local_tech_start.weekday()),
        ).fetchall()
        work_windows = [(_clock(row["starts_local"]), _clock(row["ends_local"])) for row in hour_rows]
        if not work_windows or not _contained_in_local_windows(
                occupied_start, occupied_end, technician["timezone"], work_windows):
            return "outside_working_hours"

        for table, reason in (("technician_time_off", "time_off"),
                              ("technician_schedule_blocks", "schedule_block")):
            rows = db.execute(
                f"SELECT starts_at,ends_at FROM {table} WHERE organization_id=? AND technician_id=? AND status='active'",
                (org, technician["id"]),
            ).fetchall()
            if any(_overlaps(occupied_start, occupied_end,
                             _aware_datetime(row["starts_at"], "starts_at"),
                             _aware_datetime(row["ends_at"], "ends_at")) for row in rows):
                return reason

        params: list[Any] = [org, technician["id"]]
        sql = ("SELECT starts_at,ends_at FROM appointments WHERE organization_id=? AND technician_id=? "
               "AND status IN ('confirmed','booked')")
        if exclude_appointment_id:
            sql += " AND id<>?"
            params.append(exclude_appointment_id)
        for row in db.execute(sql, params).fetchall():
            existing_start = _aware_datetime(row["starts_at"], "starts_at") - timedelta(minutes=before)
            existing_end = _aware_datetime(row["ends_at"], "ends_at") + timedelta(minutes=after)
            if _overlaps(occupied_start, occupied_end, existing_start, existing_end):
                return "existing_appointment"
        return None

    @staticmethod
    def _service_duration(config: Dict[str, Any], service_code: str) -> Optional[int]:
        entry = (config.get("service_catalog") or {}).get(service_code)
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            return None
        try:
            duration = int(entry.get("duration_minutes"))
        except (TypeError, ValueError):
            return None
        return duration if duration > 0 else None

    async def offer_slots(self, org: str, call_id: str, service_code: str, start_date: str,
                          days: int, config: Dict[str, Any], *, persist_offers: bool = True,
                          now: Optional[datetime] = None, technician_id: str = "") -> Dict[str, Any]:
        """Return deterministic capacity and an explicit fail-closed status."""
        await self.initialize()
        def query() -> Dict[str, Any]:
            scheduling = config.get("scheduling") or {}
            hours = scheduling.get("business_hours") or {}
            if not scheduling.get("enabled") or not hours:
                return {"status": "configuration_required", "slots": [],
                        "reason": "Scheduling and organization business hours must be configured"}
            catalog = config.get("service_catalog") or {}
            entry = catalog.get(service_code)
            if not isinstance(entry, dict) or not entry.get("enabled", True):
                return {"status": "manual_review_required", "slots": [],
                        "reason": "Service is not in the enabled catalog"}
            duration = self._service_duration(config, service_code)
            if duration is None:
                return {"status": "configuration_required", "slots": [],
                        "reason": "Service duration is missing or invalid"}
            tz_name = str(config.get("timezone") or "America/Vancouver")
            try:
                tz = ZoneInfo(tz_name)
            except Exception as exc:
                raise ValueError("Organization timezone is invalid") from exc
            try:
                first_day = date.fromisoformat(start_date)
            except ValueError as exc:
                raise ValueError("start_date must be YYYY-MM-DD") from exc
            try:
                notice = int(scheduling.get("minimum_notice_minutes", 120))
                interval = int(scheduling.get("slot_interval_minutes", 60))
                limit = min(max(int(scheduling.get("max_offered_slots", 3)), 1), 12)
            except (TypeError, ValueError) as exc:
                raise ValueError("Scheduling notice, interval, and offer limit must be whole numbers") from exc
            if notice < 0 or interval <= 0:
                raise ValueError("Scheduling notice must be non-negative and slot interval must be positive")
            current = (now or datetime.now(timezone.utc)).astimezone(tz)
            cutoff_raw = str(scheduling.get("same_day_cutoff") or "").strip()
            cutoff = _clock(cutoff_raw) if cutoff_raw else None
            result: list[Dict[str, Any]] = []
            diagnostics: list[Dict[str, Any]] = []
            with self._connect() as db:
                active = db.execute(
                    "SELECT id,active,timezone FROM technicians WHERE organization_id=? AND active=1 ORDER BY id",
                    (org,),
                ).fetchall()
                if not active:
                    return {"status": "configuration_required", "slots": [],
                            "reason": "No active technicians are configured"}
                eligible_ids = {row[0] for row in db.execute(
                    "SELECT technician_id FROM technician_services WHERE organization_id=? AND service_code=?",
                    (org, service_code),
                ).fetchall()}
                eligible = [tech for tech in active if tech["id"] in eligible_ids]
                if technician_id:
                    eligible = [tech for tech in eligible if tech["id"] == technician_id]
                if not eligible:
                    return {"status": "manual_review_required", "slots": [],
                            "reason": "No active technician is assigned to this service"}
                configured = [tech for tech in eligible if db.execute(
                    "SELECT 1 FROM technician_working_hours WHERE organization_id=? AND technician_id=? LIMIT 1",
                    (org, tech["id"]),
                ).fetchone()]
                if not configured:
                    return {"status": "configuration_required", "slots": [],
                            "reason": "Eligible technicians do not have recurring working hours"}
                for offset in range(max(1, min(days, 21))):
                    day = first_day + timedelta(days=offset)
                    business_windows = _daily_windows(hours.get(WEEKDAY_KEYS[day.weekday()]))
                    if not business_windows:
                        continue
                    if day == current.date() and cutoff and current.time().replace(tzinfo=None) >= cutoff:
                        diagnostics.append({"date": day.isoformat(), "reason": "same_day_cutoff"})
                        continue
                    for window_start, window_end in business_windows:
                        candidate = datetime.combine(day, window_start, tzinfo=tz)
                        closing = datetime.combine(day, window_end, tzinfo=tz)
                        while candidate + timedelta(minutes=duration) <= closing:
                            ending = candidate + timedelta(minutes=duration)
                            for tech in configured:
                                reason = "minimum_notice" if candidate < current + timedelta(minutes=notice) else self._slot_block_reason(
                                    db, org, tech, service_code, candidate, ending, config)
                                if reason:
                                    if len(diagnostics) < 200:
                                        diagnostics.append({"starts_at": candidate.isoformat(),
                                                            "technician_id": tech["id"], "reason": reason})
                                    continue
                                token = secrets.token_urlsafe(18) if persist_offers else ""
                                if persist_offers:
                                    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
                                    db.execute(
                                        "INSERT INTO offered_slots(token,organization_id,call_id,technician_id,starts_at,ends_at,service_code,expires_at,consumed_at) "
                                        "VALUES(?,?,?,?,?,?,?,?,NULL)",
                                        (token, org, call_id, tech["id"], candidate.isoformat(), ending.isoformat(),
                                         service_code, expires),
                                    )
                                slot = {"start": candidate.isoformat(), "end": ending.isoformat(),
                                        "starts_at": candidate.isoformat(), "ends_at": ending.isoformat(),
                                        "timezone": tz_name, "technician_id": tech["id"]}
                                if persist_offers:
                                    slot["slot_token"] = token
                                result.append(slot)
                                if len(result) >= limit:
                                    break
                            if len(result) >= limit:
                                break
                            candidate += timedelta(minutes=interval)
                        if len(result) >= limit:
                            break
                    if len(result) >= limit:
                        break
                status = "available" if result else "no_capacity"
                if persist_offers:
                    self._audit(db, org, call_id, "availability_retrieved", detail={
                        "service_code": service_code, "slot_count": len(result), "status": status})
                return {"status": status, "slots": result,
                        "reason": "" if result else "No technician capacity matches the request",
                        "diagnostics": diagnostics if not persist_offers else []}
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
                if str(lead["service_hint"] or "") != slot["service_code"]:
                    raise ValueError("The offered slot does not match the saved service request")
                duration = self._service_duration(config, slot["service_code"])
                starts = _aware_datetime(slot["starts_at"], "starts_at")
                ends = _aware_datetime(slot["ends_at"], "ends_at")
                if duration is None or ends - starts != timedelta(minutes=duration):
                    raise RuntimeError("Service duration configuration changed; check availability again")
                technician = db.execute(
                    "SELECT id,active,timezone FROM technicians WHERE organization_id=? AND id=?",
                    (org, slot["technician_id"]),
                ).fetchone()
                reason = "technician_unavailable" if not technician else self._slot_block_reason(
                    db, org, technician, slot["service_code"], starts, ends, config)
                if reason:
                    raise RuntimeError(f"The selected slot is no longer available ({reason})")
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
                self._audit(db, org, call_id, "booking_succeeded", "appointment", appointment_id,
                            {"job_id": job_id, "technician_id": slot["technician_id"],
                             "service_code": slot["service_code"]})
                self._audit(db, org, call_id, "technician_brief_created", "job", job_id)
            return {"appointment_id": appointment_id, "job_id": job_id, "confirmation_ref": confirmation,
                    "starts_at": slot["starts_at"], "ends_at": slot["ends_at"], "technician_brief": brief,
                    "technician_id": slot["technician_id"], "service_code": slot["service_code"],
                    "customer_phone": customer["phone"], "customer_address": f"{customer['address']}, {customer['city']} {customer['postal_code']}"}
        return await asyncio.to_thread(write)

    async def create_escalation(self, org: str, call_id: str, lead_id: str, kind: str,
                                reason: str, priority: str) -> Dict[str, Any]:
        await self.initialize()
        def write() -> Dict[str, Any]:
            with self._connect() as db:
                if lead_id and not db.execute("SELECT 1 FROM leads WHERE id=? AND organization_id=?", (lead_id, org)).fetchone():
                    raise ValueError("Lead is not valid for this organization")
                existing = db.execute(
                    "SELECT id,status FROM escalations WHERE organization_id=? AND call_id=? AND kind=? AND status='open' "
                    "ORDER BY created_at DESC LIMIT 1", (org, call_id, kind),
                ).fetchone()
                if existing:
                    return {"escalation_id": existing["id"], "status": existing["status"], "created": False}
                escalation_id = f"esc_{secrets.token_hex(8)}"
                db.execute("INSERT INTO escalations VALUES(?,?,?,?,?,?,?,?,?)", (escalation_id, org, call_id, lead_id or None, kind, reason, priority, "open", _now()))
                self._audit(db, org, call_id, "human_handoff_requested", "escalation", escalation_id, {"kind": kind, "priority": priority})
            return {"escalation_id": escalation_id, "status": "open", "created": True}
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

    async def list_upcoming_appointments(self, org: str, technician_id: str = "",
                                         limit: int = 100) -> list[Dict[str, Any]]:
        await self.initialize()
        def query() -> list[Dict[str, Any]]:
            sql = (
                "SELECT a.id,a.job_id,a.customer_id,a.technician_id,a.starts_at,a.ends_at,a.timezone,"
                "a.status,a.confirmation_ref,j.service_code FROM appointments a "
                "JOIN jobs j ON j.id=a.job_id AND j.organization_id=a.organization_id "
                "WHERE a.organization_id=? AND a.status IN ('confirmed','booked') AND a.ends_at>=?"
            )
            params: list[Any] = [org, _now()]
            if technician_id:
                sql += " AND a.technician_id=?"
                params.append(technician_id)
            sql += " ORDER BY a.starts_at LIMIT ?"
            params.append(min(max(int(limit), 1), 500))
            with self._connect() as db:
                return [dict(row) for row in db.execute(sql, params).fetchall()]
        return await asyncio.to_thread(query)

    async def change_appointment(self, org: str, call_id: str, appointment_id: str,
                                 action: str, slot_token: str = "",
                                 config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
                if config is None:
                    raise ValueError("Scheduling configuration is required for rescheduling")
                slot = db.execute(
                    "SELECT * FROM offered_slots WHERE token=? AND organization_id=? AND call_id=? AND consumed_at IS NULL",
                    (slot_token, org, call_id),
                ).fetchone()
                if not slot or datetime.fromisoformat(slot["expires_at"]) <= datetime.now(timezone.utc):
                    raise RuntimeError("The replacement slot is invalid or expired")
                job = db.execute(
                    "SELECT service_code FROM jobs WHERE id=? AND organization_id=?",
                    (appointment["job_id"], org),
                ).fetchone()
                if not job or slot["service_code"] != job["service_code"]:
                    raise ValueError("Replacement slot does not match the appointment service")
                duration = self._service_duration(config, slot["service_code"])
                starts = _aware_datetime(slot["starts_at"], "starts_at")
                ends = _aware_datetime(slot["ends_at"], "ends_at")
                if duration is None or ends - starts != timedelta(minutes=duration):
                    raise RuntimeError("Service duration configuration changed; check availability again")
                technician = db.execute(
                    "SELECT id,active,timezone FROM technicians WHERE organization_id=? AND id=?",
                    (org, slot["technician_id"]),
                ).fetchone()
                reason = "technician_unavailable" if not technician else self._slot_block_reason(
                    db, org, technician, slot["service_code"], starts, ends, config,
                    exclude_appointment_id=appointment_id)
                if reason:
                    raise RuntimeError(f"The replacement slot is no longer available ({reason})")
                db.execute(
                    "UPDATE appointments SET technician_id=?,starts_at=?,ends_at=?,timezone=?,updated_at=? "
                    "WHERE id=? AND organization_id=?",
                    (slot["technician_id"], slot["starts_at"], slot["ends_at"],
                     str(config.get("timezone") or "America/Vancouver"), now, appointment_id, org),
                )
                db.execute("UPDATE offered_slots SET consumed_at=? WHERE token=?", (now, slot_token))
                customer = db.execute(
                    "SELECT phone,address,city,postal_code FROM customers WHERE id=? AND organization_id=?",
                    (appointment["customer_id"], org),
                ).fetchone()
                self._audit(db, org, call_id, "appointment_rescheduled", "appointment", appointment_id,
                            {"technician_id": slot["technician_id"], "service_code": slot["service_code"]})
                return {"appointment_id": appointment_id, "status": "confirmed",
                        "starts_at": slot["starts_at"], "ends_at": slot["ends_at"],
                        "technician_id": slot["technician_id"], "service_code": slot["service_code"],
                        "confirmation_ref": appointment["confirmation_ref"],
                        "customer_phone": customer["phone"] if customer else "",
                        "customer_address": (f"{customer['address']}, {customer['city']} {customer['postal_code']}"
                                             if customer else "")}
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
