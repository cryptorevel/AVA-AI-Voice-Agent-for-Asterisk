from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from src.operations.service import OperationalService
from src.tools.business.operational_receptionist import (
    BookAppointmentTool,
    CheckSafetyRulesTool,
    CheckServiceAreaTool,
    ClassifyIntentTool,
    CreateCustomerTool,
    CreateLeadTool,
    GetAvailableSlotsTool,
    CreateCallbackTool,
    FinalizeReceptionistCallTool,
    ManageAppointmentTool,
)
from src.tools.context import ToolExecutionContext


def operational_config(db_path, *, scheduling=True):
    return {
        "tools": {
            "operational_receptionist": {
                "enabled": True,
                "organization_id": "coreline",
                "company_name": "Coreline Comfort Solution",
                "database_path": str(db_path),
                "timezone": "America/Vancouver",
                "service_catalog": {
                    "furnace_repair": {"enabled": True, "duration_minutes": 120},
                },
                "service_area": {
                    "supported_cities": ["Vancouver", "Burnaby"],
                    "excluded_cities": ["Victoria"],
                    "supported_postal_prefixes": ["V5"],
                },
                "scheduling": {
                    "enabled": scheduling,
                    "minimum_notice_minutes": 0,
                    "slot_interval_minutes": 120,
                    "max_offered_slots": 3,
                    "business_hours": {day: ["09:00", "17:00"] for day in
                                       ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                },
                "sms": {"enabled": False, "provider": "twilio"},
            }
        }
    }


def context(db_path, state=None, *, scheduling=True, call_id="call-1"):
    session = SimpleNamespace(operational_state=dict(state or {}), conversation_history=[])
    store = AsyncMock()
    store.get_by_call_id.return_value = session

    async def upsert(updated):
        nonlocal session
        session = updated

    store.upsert_call.side_effect = upsert
    return ToolExecutionContext(
        call_id=call_id,
        caller_number="+16045550101",
        session_store=store,
        config=operational_config(db_path, scheduling=scheduling),
    ), session


@pytest.mark.asyncio
async def test_customer_create_match_and_duplicate_prevention(tmp_path):
    db_path = tmp_path / "operations.db"
    ctx, _ = context(db_path)
    payload = {"first_name": "Sara", "last_name": "Chen", "phone": "604-555-0101",
               "address": "123 Main St", "city": "Burnaby", "postal_code": "V5A 1A1"}
    first = await CreateCustomerTool().execute(payload, ctx)
    second = await CreateCustomerTool().execute({**payload, "address": "125 Main St"}, ctx)
    assert first["status"] == "success" and first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert second["data"]["customer_id"] == first["data"]["customer_id"]
    found = await OperationalService(str(db_path)).find_customer("coreline", "+16045550101")
    assert found["address"] == "125 Main St"


@pytest.mark.asyncio
async def test_tenant_isolation(tmp_path):
    service = OperationalService(str(tmp_path / "operations.db"))
    created = await service.upsert_customer("org-a", "call-a", {
        "first_name": "A", "phone": "+16045550101", "address": "1 A St",
        "city": "Vancouver", "postal_code": "V5A1A1"})
    assert await service.find_customer("org-b", "+16045550101") is None
    lead = await service.upsert_lead("org-b", "call-b", {
        "customer_id": created["customer_id"], "caller_number": "+16045550101",
        "service_category": "hvac", "service_hint": "furnace_repair",
        "issue_description": "No heat", "urgency": "high"})
    with pytest.raises(ValueError):
        await service.book("org-b", "call-b", "bad-token", created["customer_id"],
                           lead["lead_id"], {}, operational_config(tmp_path)["tools"]["operational_receptionist"])


@pytest.mark.asyncio
async def test_lead_is_idempotent_per_call(tmp_path):
    ctx, _ = context(tmp_path / "operations.db")
    args = {"service_category": "hvac", "service_hint": "furnace_repair",
            "issue_description": "No heat", "urgency": "high"}
    first = await CreateLeadTool().execute(args, ctx)
    second = await CreateLeadTool().execute({**args, "issue_description": "No heat since yesterday"}, ctx)
    assert first["data"]["created"] is True
    assert second["data"]["created"] is False
    assert first["data"]["lead_id"] == second["data"]["lead_id"]


@pytest.mark.asyncio
async def test_safety_trigger_and_normal_path(tmp_path):
    clear_ctx, clear_session = context(tmp_path / "clear.db")
    clear = await CheckSafetyRulesTool().execute({"caller_statement": "furnace has no heat"}, clear_ctx)
    assert clear["safety_state"] == "CLEAR"
    assert clear_session.operational_state["safety_state"] == "clear"

    danger_ctx, danger_session = context(tmp_path / "danger.db")
    danger = await CheckSafetyRulesTool().execute({"caller_statement": "بوی گاز میاد"}, danger_ctx)
    assert danger["safety_state"] == "EMERGENCY"
    assert danger["stop_normal_flow"] is True
    assert danger_session.operational_state["urgency"] == "emergency"


@pytest.mark.asyncio
async def test_service_area_three_states(tmp_path):
    tool = CheckServiceAreaTool()
    supported, _ = context(tmp_path / "supported.db")
    outside, _ = context(tmp_path / "outside.db")
    review, _ = context(tmp_path / "review.db")
    assert (await tool.execute({"city": "Burnaby", "postal_code": "V5A1A1"}, supported))["service_area_state"] == "SUPPORTED"
    assert (await tool.execute({"city": "Victoria", "postal_code": "V8V1A1"}, outside))["service_area_state"] == "OUTSIDE_SERVICE_AREA"
    assert (await tool.execute({"city": "Surrey", "postal_code": "V3T1A1"}, review))["service_area_state"] == "REQUIRES_REVIEW"


@pytest.mark.asyncio
async def test_intent_schema_and_language_state(tmp_path):
    ctx, session = context(tmp_path / "intent.db")
    result = await ClassifyIntentTool().execute({
        "intent": "hvac_service", "confidence": 0.96, "service_hint": "furnace_repair",
        "urgency_hint": "high", "detected_language": "fa-IR"}, ctx)
    assert result["needs_clarification"] is False
    assert session.operational_state["detected_language"] == "fa-IR"
    low = await ClassifyIntentTool().execute({
        "intent": "other_unknown", "confidence": 0.4, "service_hint": "",
        "urgency_hint": "standard", "detected_language": "mixed-fa-en"}, ctx)
    assert low["needs_clarification"] is True


async def create_booking_prerequisites(db_path, call_id):
    service = OperationalService(str(db_path))
    await service.upsert_technician("coreline", {
        "id": "tech-hvac", "display_name": "tech-hvac", "active": True,
        "timezone": "America/Vancouver", "service_ids": ["furnace_repair"],
        "working_hours": {day: [["09:00", "17:00"]] for day in
                          ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
    }, operational_config(db_path)["tools"]["operational_receptionist"]["service_catalog"])
    ctx, session = context(db_path, {"safety_state": "clear", "service_area_state": "SUPPORTED"}, call_id=call_id)
    customer = await CreateCustomerTool().execute({
        "first_name": "Sam", "phone": "+16045550101", "address": "1 Main St",
        "city": "Burnaby", "postal_code": "V5A1A1"}, ctx)
    lead = await CreateLeadTool().execute({
        "customer_id": customer["data"]["customer_id"], "service_category": "hvac",
        "service_hint": "furnace_repair", "issue_description": "No heat", "urgency": "high"}, ctx)
    return ctx, session, customer["data"]["customer_id"], lead["data"]["lead_id"]


@pytest.mark.asyncio
async def test_real_slot_booking_and_sms_failure_truthfulness(tmp_path):
    db_path = tmp_path / "booking.db"
    ctx, session, customer_id, lead_id = await create_booking_prerequisites(db_path, "call-book")
    start_date = (datetime.now(ZoneInfo("America/Vancouver")) + timedelta(days=1)).date().isoformat()
    slots = await GetAvailableSlotsTool().execute({"service_code": "furnace_repair", "start_date": start_date}, ctx)
    assert slots["status"] == "success" and slots["data"]["slots"]
    selected = slots["data"]["slots"][0]
    result = await BookAppointmentTool().execute({
        "slot_token": selected["slot_token"], "customer_id": customer_id, "lead_id": lead_id,
        "issue_description": "Furnace runs but makes no heat", "urgency": "high"}, ctx)
    assert result["booking_confirmed"] is True
    assert result["sms"]["sent"] is False
    assert result["sms"]["send_status"] == "not_configured"
    assert session.operational_state["booking_state"] == "confirmed"


@pytest.mark.asyncio
async def test_booking_conflict_and_stale_slot_fail_closed(tmp_path):
    db_path = tmp_path / "conflict.db"
    first_ctx, _, first_customer, first_lead = await create_booking_prerequisites(db_path, "call-first")
    second_ctx, _, second_customer, second_lead = await create_booking_prerequisites(db_path, "call-second")
    start_date = (datetime.now(ZoneInfo("America/Vancouver")) + timedelta(days=1)).date().isoformat()
    first_slots = await GetAvailableSlotsTool().execute({"service_code": "furnace_repair", "start_date": start_date}, first_ctx)
    second_slots = await GetAvailableSlotsTool().execute({"service_code": "furnace_repair", "start_date": start_date}, second_ctx)
    one = first_slots["data"]["slots"][0]; two = second_slots["data"]["slots"][0]
    assert one["starts_at"] == two["starts_at"]
    booked = await BookAppointmentTool().execute({"slot_token": one["slot_token"], "customer_id": first_customer,
        "lead_id": first_lead, "issue_description": "No heat", "urgency": "high"}, first_ctx)
    conflict = await BookAppointmentTool().execute({"slot_token": two["slot_token"], "customer_id": second_customer,
        "lead_id": second_lead, "issue_description": "No heat", "urgency": "high"}, second_ctx)
    assert booked["booking_confirmed"] is True
    assert conflict["booking_confirmed"] is False

    with OperationalService(str(db_path))._connect() as db:
        db.execute("UPDATE offered_slots SET expires_at=? WHERE token=?",
                   ((datetime.now(ZoneInfo("UTC")) - timedelta(minutes=1)).isoformat(), second_slots["data"]["slots"][1]["slot_token"]))
    stale = await BookAppointmentTool().execute({"slot_token": second_slots["data"]["slots"][1]["slot_token"],
        "customer_id": second_customer, "lead_id": second_lead, "issue_description": "No heat", "urgency": "high"}, second_ctx)
    assert stale["booking_confirmed"] is False


@pytest.mark.asyncio
async def test_scheduling_unavailable_is_truthful(tmp_path):
    ctx, _ = context(tmp_path / "disabled.db", {"safety_state": "clear", "service_area_state": "SUPPORTED"}, scheduling=False)
    result = await GetAvailableSlotsTool().execute({"service_code": "furnace_repair", "start_date": "2026-09-06"}, ctx)
    assert result["status"] == "success"
    assert result["data"]["status"] == "configuration_required"
    assert result["data"]["slots"] == []
    assert "office follow up" in result["message"]


@pytest.mark.asyncio
async def test_callback_and_final_artifacts_are_persisted(tmp_path):
    db_path = tmp_path / "handoff.db"
    ctx, session = context(db_path)
    callback = await CreateCallbackTool().execute({
        "reason": "Caller requested a person", "priority": "normal", "kind": "human_request"}, ctx)
    assert callback["status"] == "success"
    session.conversation_history = [{"role": "user", "content": "I need a person"}]
    final = await FinalizeReceptionistCallTool().execute({
        "verified_facts": {"intent": "existing_customer_support"},
        "ai_summary": "Caller requested human support.", "technician_brief": "",
        "languages": ["en"], "outcome": "callback"}, ctx)
    assert final["status"] == "success"
    with OperationalService(str(db_path))._connect() as db:
        assert db.execute("SELECT count(*) FROM escalations").fetchone()[0] == 1
        assert db.execute("SELECT outcome FROM call_artifacts").fetchone()[0] == "callback"


@pytest.mark.asyncio
async def test_existing_appointment_lookup_and_cancel(tmp_path):
    db_path = tmp_path / "manage.db"
    ctx, _, customer_id, lead_id = await create_booking_prerequisites(db_path, "call-create")
    start_date = (datetime.now(ZoneInfo("America/Vancouver")) + timedelta(days=1)).date().isoformat()
    slots = await GetAvailableSlotsTool().execute({"service_code": "furnace_repair", "start_date": start_date}, ctx)
    booked = await BookAppointmentTool().execute({
        "slot_token": slots["data"]["slots"][0]["slot_token"], "customer_id": customer_id,
        "lead_id": lead_id, "issue_description": "No heat", "urgency": "high"}, ctx)
    manage_ctx, _ = context(db_path, call_id="call-manage")
    lookup = await ManageAppointmentTool().execute({"action": "lookup", "phone": "+16045550101"}, manage_ctx)
    assert lookup["data"]["appointments"][0]["id"] == booked["data"]["appointment_id"]
    cancelled = await ManageAppointmentTool().execute({
        "action": "cancel", "appointment_id": booked["data"]["appointment_id"]}, manage_ctx)
    assert cancelled["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_sms_success_records_provider_id(tmp_path, monkeypatch):
    class Response:
        status = 201
        async def json(self, content_type=None):
            return {"sid": "SM-safe-test", "status": "queued"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-test")
    monkeypatch.setenv("TWILIO_SMS_FROM", "+16045550100")
    monkeypatch.setattr("src.operations.service.aiohttp.ClientSession", Session)
    service = OperationalService(str(tmp_path / "sms.db"))
    result = await service.send_sms("coreline", "appt-test", "+16045550101", "Confirmed", {
        "sms": {"enabled": True, "provider": "twilio"}})
    assert result["sent"] is True
    assert result["send_status"] == "queued"


def capacity_settings(db_path):
    return operational_config(db_path)["tools"]["operational_receptionist"]


async def configure_capacity(db_path, *, technician_id="tech-1", active=True,
                             services=None, hours=None, timezone="America/Vancouver"):
    service = OperationalService(str(db_path))
    await service.upsert_technician("coreline", {
        "id": technician_id,
        "display_name": technician_id,
        "active": active,
        "timezone": timezone,
        "service_ids": services if services is not None else ["furnace_repair"],
        "working_hours": hours if hours is not None else {
            day: [["09:00", "17:00"]] for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        },
    }, capacity_settings(db_path)["service_catalog"])
    return service


@pytest.mark.asyncio
async def test_technician_crud_assignments_activation_and_tenant_isolation(tmp_path):
    db_path = tmp_path / "technicians.db"
    service = await configure_capacity(db_path, services=["furnace_repair"])
    created = await service.get_technician("coreline", "tech-1")
    assert created["display_name"] == "tech-1"
    assert created["active"] is True
    assert created["service_ids"] == ["furnace_repair"]
    assert await service.get_technician("other-org", "tech-1") is None

    updated = await service.upsert_technician("coreline", {
        **created, "display_name": "North Team", "active": False,
    }, capacity_settings(db_path)["service_catalog"])
    assert updated["display_name"] == "North Team"
    assert updated["active"] is False
    assert len(await service.list_technicians("coreline")) == 1
    assert await service.list_technicians("other-org") == []


@pytest.mark.asyncio
async def test_split_working_hours_day_off_and_outside_interval(tmp_path):
    db_path = tmp_path / "hours.db"
    service = await configure_capacity(db_path, hours={"mon": [["09:00", "12:00"], ["13:00", "17:00"]]})
    config = capacity_settings(db_path)
    result = await service.offer_slots(
        "coreline", "call-hours", "furnace_repair", "2026-09-07", 1, config,
        persist_offers=False, now=datetime(2026, 9, 1, tzinfo=ZoneInfo("America/Vancouver")))
    starts = {slot["start"][11:16] for slot in result["slots"]}
    assert result["status"] == "available"
    assert starts == {"09:00", "13:00", "15:00"}

    day_off = await service.offer_slots(
        "coreline", "call-off", "furnace_repair", "2026-09-08", 1, config,
        persist_offers=False, now=datetime(2026, 9, 1, tzinfo=ZoneInfo("America/Vancouver")))
    assert day_off["status"] == "no_capacity"
    assert day_off["slots"] == []


@pytest.mark.asyncio
async def test_time_off_and_schedule_blocks_remove_capacity_and_can_be_cancelled(tmp_path):
    db_path = tmp_path / "exceptions.db"
    service = await configure_capacity(db_path)
    config = capacity_settings(db_path)
    time_off = await service.set_technician_exception("coreline", "tech-1", "time_off", {
        "start_datetime": "2026-09-07T09:00:00-07:00",
        "end_datetime": "2026-09-07T13:00:00-07:00", "reason": "training"})
    block = await service.set_technician_exception("coreline", "tech-1", "block", {
        "start_datetime": "2026-09-07T15:00:00-07:00",
        "end_datetime": "2026-09-07T17:00:00-07:00", "reason": "vehicle service"})
    result = await service.offer_slots(
        "coreline", "call-exceptions", "furnace_repair", "2026-09-07", 1, config,
        persist_offers=False, now=datetime(2026, 9, 1, tzinfo=ZoneInfo("America/Vancouver")))
    assert [slot["start"][11:16] for slot in result["slots"]] == ["13:00"]
    assert {item["reason"] for item in result["diagnostics"]} >= {"time_off", "schedule_block"}

    await service.cancel_technician_exception("coreline", "tech-1", "time_off", time_off["id"])
    await service.cancel_technician_exception("coreline", "tech-1", "block", block["id"])
    released = await service.offer_slots(
        "coreline", "call-released", "furnace_repair", "2026-09-07", 1, config,
        persist_offers=False, now=datetime(2026, 9, 1, tzinfo=ZoneInfo("America/Vancouver")))
    assert len(released["slots"]) == 3


@pytest.mark.asyncio
async def test_availability_fail_closed_statuses_for_capacity_eligibility_and_duration(tmp_path):
    db_path = tmp_path / "fail-closed.db"
    service = OperationalService(str(db_path))
    config = capacity_settings(db_path)
    no_technicians = await service.offer_slots("coreline", "call-none", "furnace_repair", "2026-09-07", 1, config)
    assert no_technicians["status"] == "configuration_required"

    await configure_capacity(db_path, services=[])
    no_eligible = await service.offer_slots("coreline", "call-skill", "furnace_repair", "2026-09-07", 1, config)
    assert no_eligible["status"] == "manual_review_required"

    missing = {**config, "service_catalog": {"furnace_repair": {"enabled": True}}}
    await configure_capacity(db_path)
    no_duration = await service.offer_slots("coreline", "call-duration", "furnace_repair", "2026-09-07", 1, missing)
    assert no_duration["status"] == "configuration_required"

    too_long = {**config, "service_catalog": {"furnace_repair": {"enabled": True, "duration_minutes": 600}}}
    no_fit = await service.offer_slots(
        "coreline", "call-long", "furnace_repair", "2026-09-07", 1, too_long,
        now=datetime(2026, 9, 1, tzinfo=ZoneInfo("America/Vancouver")))
    assert no_fit["status"] == "no_capacity"


@pytest.mark.asyncio
async def test_buffers_minimum_notice_and_same_day_cutoff_are_deterministic(tmp_path):
    db_path = tmp_path / "rules.db"
    service = await configure_capacity(db_path)
    config = capacity_settings(db_path)
    config["scheduling"] = {**config["scheduling"], "minimum_notice_minutes": 180,
                            "travel_buffer_before_minutes": 30, "travel_buffer_after_minutes": 30,
                            "preparation_buffer_minutes": 30, "same_day_cutoff": "16:00"}
    result = await service.offer_slots(
        "coreline", "call-rules", "furnace_repair", "2026-09-07", 1, config,
        persist_offers=False, now=datetime(2026, 9, 7, 7, 30, tzinfo=ZoneInfo("America/Vancouver")))
    assert [slot["start"][11:16] for slot in result["slots"]] == ["11:00", "13:00"]

    cutoff = await service.offer_slots(
        "coreline", "call-cutoff", "furnace_repair", "2026-09-07", 1, config,
        persist_offers=False, now=datetime(2026, 9, 7, 16, 1, tzinfo=ZoneInfo("America/Vancouver")))
    assert cutoff["status"] == "no_capacity"
    assert cutoff["diagnostics"] == [{"date": "2026-09-07", "reason": "same_day_cutoff"}]


@pytest.mark.asyncio
async def test_inactive_and_cross_org_technicians_never_supply_slots(tmp_path):
    db_path = tmp_path / "isolation.db"
    service = await configure_capacity(db_path, active=False)
    config = capacity_settings(db_path)
    inactive = await service.offer_slots("coreline", "call-inactive", "furnace_repair", "2026-09-07", 1, config)
    assert inactive["status"] == "configuration_required"
    await service.upsert_technician("other-org", {
        "id": "tech-other", "display_name": "Other", "active": True,
        "timezone": "America/Vancouver", "service_ids": ["furnace_repair"],
        "working_hours": {"mon": [["09:00", "17:00"]]},
    }, config["service_catalog"])
    isolated = await service.offer_slots("coreline", "call-isolated", "furnace_repair", "2026-09-07", 1, config)
    assert isolated["status"] == "configuration_required"


@pytest.mark.asyncio
async def test_failed_booking_rolls_back_and_cancelled_appointment_releases_capacity(tmp_path):
    db_path = tmp_path / "rollback.db"
    ctx, _, customer_id, lead_id = await create_booking_prerequisites(db_path, "call-rollback")
    start_date = (datetime.now(ZoneInfo("America/Vancouver")) + timedelta(days=1)).date().isoformat()
    offered = await GetAvailableSlotsTool().execute({"service_code": "furnace_repair", "start_date": start_date}, ctx)
    token = offered["data"]["slots"][0]["slot_token"]
    service = OperationalService(str(db_path))
    with pytest.raises(ValueError):
        await service.book("other-org", "call-rollback", token, customer_id, lead_id, {}, capacity_settings(db_path))
    with service._connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM appointments").fetchone()[0] == 0

    booked = await service.book("coreline", "call-rollback", token, customer_id, lead_id,
                                {"issue_description": "No heat", "urgency": "high"}, capacity_settings(db_path))
    before_cancel = booked["starts_at"]
    await service.change_appointment("coreline", "call-cancel", booked["appointment_id"], "cancel")
    released = await service.offer_slots("coreline", "call-after-cancel", "furnace_repair", start_date, 1,
                                         capacity_settings(db_path))
    assert before_cancel in {slot["starts_at"] for slot in released["slots"]}


@pytest.mark.asyncio
async def test_reschedule_revalidates_service_eligibility_and_releases_old_slot(tmp_path):
    db_path = tmp_path / "reschedule.db"
    ctx, _, customer_id, lead_id = await create_booking_prerequisites(db_path, "call-original")
    start_date = (datetime.now(ZoneInfo("America/Vancouver")) + timedelta(days=1)).date().isoformat()
    first = await GetAvailableSlotsTool().execute({"service_code": "furnace_repair", "start_date": start_date}, ctx)
    booked = await BookAppointmentTool().execute({
        "slot_token": first["data"]["slots"][0]["slot_token"], "customer_id": customer_id,
        "lead_id": lead_id, "issue_description": "No heat", "urgency": "high"}, ctx)
    old_start = booked["data"]["starts_at"]

    manage_ctx, _ = context(db_path, {"safety_state": "clear", "service_area_state": "SUPPORTED"}, call_id="call-reschedule")
    replacement = await GetAvailableSlotsTool().execute(
        {"service_code": "furnace_repair", "start_date": start_date}, manage_ctx)
    replacement_slot = next(slot for slot in replacement["data"]["slots"] if slot["starts_at"] != old_start)
    moved = await ManageAppointmentTool().execute({
        "action": "reschedule", "appointment_id": booked["data"]["appointment_id"],
        "slot_token": replacement_slot["slot_token"]}, manage_ctx)
    assert moved["status"] == "success"
    assert moved["data"]["technician_id"] == "tech-hvac"
    assert moved["sms"]["sent"] is False
    assert moved["escalation"]["status"] == "open"
    available_again = await OperationalService(str(db_path)).offer_slots(
        "coreline", "call-old-slot", "furnace_repair", start_date, 1, capacity_settings(db_path))
    assert old_start in {slot["starts_at"] for slot in available_again["slots"]}
