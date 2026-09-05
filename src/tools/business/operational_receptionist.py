"""Validated tools for persisted service intake, booking, messaging, and handoff."""

from __future__ import annotations

from typing import Any, Dict

import structlog

from src.operations.service import get_operational_service, normalize_postal
from src.tools.base import Tool, ToolCategory, ToolDefinition
from src.tools.context import ToolExecutionContext, resolve_scoped_tool_config

logger = structlog.get_logger(__name__)

INTENTS = ["hvac_service", "plumbing_service", "electrical_service", "existing_appointment",
           "reschedule_appointment", "cancel_appointment", "billing_invoice",
           "existing_customer_support", "emergency_urgent", "sales_estimate", "other_unknown"]
URGENCIES = ["emergency", "high", "standard_urgent", "standard"]


class OperationalTool(Tool):
    tool_name = ""
    schema: Dict[str, Any] = {}
    description = ""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.tool_name, description=self.description,
                              category=ToolCategory.BUSINESS, input_schema=self.schema,
                              max_execution_time=15)

    def _parts(self, context: ToolExecutionContext) -> tuple[Dict[str, Any], str, Any]:
        config = resolve_scoped_tool_config(context, "operational_receptionist", self._load_config)
        if not config.get("enabled"):
            raise RuntimeError("Operational receptionist tools are disabled")
        organization_id = str(config.get("organization_id") or "").strip()
        if not organization_id:
            raise RuntimeError("Operational receptionist organization is not configured")
        database_path = str(config.get("database_path") or "/app/data/operator/operations.db")
        return config, organization_id, get_operational_service(database_path)

    async def _update_state(self, context: ToolExecutionContext, **values: Any) -> None:
        if not context.session_store:
            return
        session = await context.get_session()
        state = dict(getattr(session, "operational_state", {}) or {})
        state.update(values)
        await context.update_session(operational_state=state)

    @staticmethod
    def _error(exc: Exception) -> Dict[str, Any]:
        return {"status": "error", "error_code": type(exc).__name__, "message": str(exc)}


class ClassifyIntentTool(OperationalTool):
    tool_name = "classify_intent"
    description = "Record structured caller intent. If confidence is below 0.70, ask one concise clarification question."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "service_hint": {"type": "string"}, "urgency_hint": {"type": "string", "enum": URGENCIES},
        "detected_language": {"type": "string"}},
        "required": ["intent", "confidence", "service_hint", "urgency_hint", "detected_language"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            confidence = float(parameters["confidence"])
            if parameters.get("intent") not in INTENTS or not 0 <= confidence <= 1:
                raise ValueError("Invalid intent classification")
            await self._update_state(context, intent=parameters["intent"], intent_confidence=confidence,
                                     service=parameters["service_hint"], urgency=parameters["urgency_hint"],
                                     detected_language=parameters["detected_language"])
            await service.audit(org, context.call_id, "intent_classified", {
                "intent": parameters["intent"], "confidence": confidence,
                "service_hint": parameters["service_hint"], "urgency": parameters["urgency_hint"],
                "language": parameters["detected_language"]})
            needs_clarification = confidence < 0.70
            return {"status": "success", "data": dict(parameters), "needs_clarification": needs_clarification,
                    "message": "Ask one concise clarification question." if needs_clarification else "Intent recorded."}
        except Exception as exc:
            return self._error(exc)


class IdentifyCustomerTool(OperationalTool):
    tool_name = "identify_customer"
    description = "Find an existing customer by caller-confirmed phone number within this organization."
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"phone": {"type": "string"}}, "required": ["phone"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            customer = await service.find_customer(org, str(parameters.get("phone") or context.caller_number or ""))
            if not customer:
                return {"status": "success", "found": False, "data": {}, "message": "No matching customer. Collect the required details."}
            await self._update_state(context, customer_id=customer["id"])
            await service.audit(org, context.call_id, "customer_matched", {"customer_id": customer["id"]})
            return {"status": "success", "found": True, "data": customer,
                    "message": "Customer found; confirm the address before using it."}
        except Exception as exc:
            return self._error(exc)


class CreateCustomerTool(OperationalTool):
    tool_name = "create_customer"
    description = "Create or update a customer after confirming identity and service address."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "first_name": {"type": "string"}, "last_name": {"type": "string"}, "phone": {"type": "string"},
        "email": {"type": "string"}, "address": {"type": "string"}, "city": {"type": "string"},
        "postal_code": {"type": "string"}},
        "required": ["first_name", "phone", "address", "city", "postal_code"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            result = await service.upsert_customer(org, context.call_id, parameters)
            await self._update_state(context, customer_id=result["customer_id"])
            return {"status": "success", "data": result, "message": "Customer details saved."}
        except Exception as exc:
            return self._error(exc)


class CreateLeadTool(OperationalTool):
    tool_name = "create_lead"
    description = "Create or update the single actionable lead for this call. Call before availability lookup."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "customer_id": {"type": "string"}, "service_category": {"type": "string"},
        "service_hint": {"type": "string"}, "issue_description": {"type": "string"},
        "urgency": {"type": "string", "enum": URGENCIES}, "ai_summary": {"type": "string"}},
        "required": ["service_category", "service_hint", "issue_description", "urgency"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            payload = dict(parameters); payload["caller_number"] = context.caller_number or ""
            result = await service.upsert_lead(org, context.call_id, payload)
            await self._update_state(context, lead_id=result["lead_id"], service=parameters["service_hint"],
                                     urgency=parameters["urgency"], qualification_state="lead_saved")
            return {"status": "success", "data": result, "message": "Service request saved."}
        except Exception as exc:
            return self._error(exc)


class GetServiceCatalogTool(OperationalTool):
    tool_name = "get_service_catalog"
    description = "Return configured services the company actually offers. Never offer a service absent from this result."
    schema = {"type": "object", "additionalProperties": False, "properties": {}}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            config, _, _ = self._parts(context)
            catalog = {key: value for key, value in (config.get("service_catalog") or {}).items()
                       if isinstance(value, dict) and value.get("enabled", True)}
            return {"status": "success", "data": catalog, "message": "Configured service catalog returned."}
        except Exception as exc:
            return self._error(exc)


class CheckSafetyRulesTool(OperationalTool):
    tool_name = "check_safety_rules"
    description = "Apply deterministic safety rules to the caller's own description before booking."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "caller_statement": {"type": "string"}, "reported_gas_smell": {"type": "boolean"},
        "reported_co_alarm_or_symptoms": {"type": "boolean"}, "reported_smoke_fire_sparks": {"type": "boolean"},
        "reported_major_flooding_or_water_on_electrical": {"type": "boolean"}},
        "required": ["caller_statement"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            text = str(parameters.get("caller_statement") or "").casefold()
            rules = [
                ("gas", bool(parameters.get("reported_gas_smell")) or any(x in text for x in ("gas smell", "smell gas", "بوی گاز")),
                 "Please leave the home if it's safe to do so, avoid switches, flames, or anything that could create a spark, and contact the gas utility or emergency services from a safe location."),
                ("carbon_monoxide", bool(parameters.get("reported_co_alarm_or_symptoms")) or any(x in text for x in ("carbon monoxide", "co alarm", "مونوکسید کربن")),
                 "Please leave the building and get to fresh air, then contact emergency services or the appropriate emergency authority."),
                ("fire_electrical", bool(parameters.get("reported_smoke_fire_sparks")) or any(x in text for x in ("smoke", "sparks", "burning electrical", "دود", "جرقه", "بوی سوختگی")),
                 "Please shut the system off only if you can do so safely, and contact emergency services if there is smoke, fire, or an immediate electrical hazard."),
                ("water_electrical", bool(parameters.get("reported_major_flooding_or_water_on_electrical")) or any(x in text for x in ("water on electrical", "major flooding", "burst pipe", "آب روی برق", "لوله ترکیده")),
                 "Do not touch electrical equipment near the water. Move to a safe location and contact emergency services if there is immediate danger."),
            ]
            match = next((item for item in rules if item[1]), None)
            if not match:
                await self._update_state(context, safety_state="clear")
                await service.audit(org, context.call_id, "safety_checked", {"triggered": False})
                return {"status": "success", "safety_state": "CLEAR", "stop_normal_flow": False,
                        "message": "No safety trigger identified."}
            await self._update_state(context, safety_state=match[0], urgency="emergency")
            escalation = await service.create_escalation(org, context.call_id, "", "safety", match[0], "emergency")
            await service.audit(org, context.call_id, "safety_escalation", {"rule": match[0]})
            return {"status": "success", "safety_state": "EMERGENCY", "trigger": match[0],
                    "stop_normal_flow": True, "approved_guidance": match[2], "escalation": escalation,
                    "message": match[2]}
        except Exception as exc:
            return self._error(exc)


class CheckServiceAreaTool(OperationalTool):
    tool_name = "check_service_area"
    description = "Deterministically check a confirmed city and postal code before offering appointments."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "city": {"type": "string"}, "postal_code": {"type": "string"}}, "required": ["city", "postal_code"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            config, org, service = self._parts(context)
            area = config.get("service_area") or {}
            city = str(parameters.get("city") or "").strip().casefold()
            postal = normalize_postal(str(parameters.get("postal_code") or ""))
            included = {str(x).strip().casefold() for x in area.get("supported_cities") or []}
            excluded = {str(x).strip().casefold() for x in area.get("excluded_cities") or []}
            prefixes = tuple(str(x).strip().upper() for x in area.get("supported_postal_prefixes") or [])
            state = "OUTSIDE_SERVICE_AREA" if city in excluded else (
                "SUPPORTED" if city in included or (prefixes and postal.startswith(prefixes)) else "REQUIRES_REVIEW")
            await self._update_state(context, service_area_state=state)
            await service.audit(org, context.call_id, "service_area_checked", {"state": state})
            return {"status": "success", "service_area_state": state, "booking_allowed": state == "SUPPORTED",
                    "message": "Service area result recorded."}
        except Exception as exc:
            return self._error(exc)


class GetAvailableSlotsTool(OperationalTool):
    tool_name = "get_available_slots"
    description = "Return short-lived appointment offers from configured technician capacity. Never invent or alter returned slots."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "service_code": {"type": "string"}, "start_date": {"type": "string"},
        "days_to_search": {"type": "integer", "minimum": 1, "maximum": 21}},
        "required": ["service_code", "start_date"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            config, org, service = self._parts(context)
            state = dict(getattr(await context.get_session(), "operational_state", {}) or {})
            if state.get("safety_state") not in {"clear", None}:
                raise RuntimeError("Normal booking is stopped by a safety escalation")
            if state.get("service_area_state") != "SUPPORTED":
                raise RuntimeError("Service area must be confirmed before availability")
            slots = await service.offer_slots(org, context.call_id, str(parameters["service_code"]),
                                              str(parameters["start_date"]), int(parameters.get("days_to_search") or 7), config)
            await self._update_state(context, booking_state="slots_offered")
            return {"status": "success", "data": {"slots": slots},
                    "message": "Offer only these slots." if slots else "No slots found; create a callback request."}
        except Exception as exc:
            return self._error(exc)


class BookAppointmentTool(OperationalTool):
    tool_name = "book_appointment"
    description = "Revalidate a returned slot and transactionally create a job and appointment. Confirm only if booking_confirmed is true."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "slot_token": {"type": "string"}, "customer_id": {"type": "string"}, "lead_id": {"type": "string"},
        "issue_description": {"type": "string"}, "urgency": {"type": "string", "enum": URGENCIES},
        "safety_notes": {"type": "string"}, "access_notes": {"type": "string"}},
        "required": ["slot_token", "customer_id", "lead_id", "issue_description", "urgency"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            config, org, service = self._parts(context)
            booked = await service.book(org, context.call_id, str(parameters["slot_token"]),
                                        str(parameters["customer_id"]), str(parameters["lead_id"]), parameters, config)
            template = str((config.get("sms") or {}).get("confirmation_template") or
                           "{company}\nYour service appointment is confirmed for {starts_at}.\nService address: {address}.\nConfirmation: {confirmation_ref}")
            body = template.format(company=str(config.get("company_name") or "Coreline Comfort Solution"),
                                   starts_at=booked["starts_at"], ends_at=booked["ends_at"],
                                   address=booked["customer_address"], confirmation_ref=booked["confirmation_ref"])
            sms = await service.send_sms(org, booked["appointment_id"], booked["customer_phone"], body, config)
            escalation = None
            if not sms["sent"]:
                escalation = await service.create_escalation(
                    org, context.call_id, str(parameters["lead_id"]), "sms_failure",
                    "Appointment confirmation SMS was not sent", "normal")
            await self._update_state(context, job_id=booked["job_id"], appointment_id=booked["appointment_id"],
                                     booking_state="confirmed", summary_state="pending",
                                     technician_brief=booked["technician_brief"])
            return {"status": "success", "booking_confirmed": True, "data": booked, "sms": sms,
                    "escalation": escalation,
                    "message": "Appointment confirmed and SMS sent." if sms["sent"] else
                               "Appointment confirmed, but SMS was not sent; say that accurately."}
        except Exception as exc:
            try:
                _, org, service = self._parts(context)
                await service.audit(org, context.call_id, "booking_failed", {"error_type": type(exc).__name__})
                await service.create_escalation(
                    org, context.call_id, str(parameters.get("lead_id") or ""), "booking_failure",
                    "Automated booking did not complete", "high")
                await self._update_state(context, booking_state="failed")
            except Exception:
                logger.debug("Unable to record booking failure", call_id=context.call_id)
            result = self._error(exc); result["booking_confirmed"] = False
            result["message"] = "I wasn't able to complete that booking yet. I'll make sure the office receives the request."
            return result


class CreateCallbackTool(OperationalTool):
    tool_name = "create_callback"
    description = "Create a real callback/escalation record when a human is requested or booking cannot complete."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "lead_id": {"type": "string"}, "reason": {"type": "string"},
        "priority": {"type": "string", "enum": ["emergency", "high", "normal"]},
        "kind": {"type": "string", "enum": ["callback", "dispatcher", "billing", "complaint", "warranty", "human_request", "unsupported"]}},
        "required": ["reason", "priority", "kind"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            result = await service.create_escalation(org, context.call_id, str(parameters.get("lead_id") or ""),
                                                     str(parameters["kind"]), str(parameters["reason"]), str(parameters["priority"]))
            await self._update_state(context, handoff_state="callback_created")
            return {"status": "success", "data": result, "message": "A callback request was created for the Coreline team."}
        except Exception as exc:
            return self._error(exc)


class ManageAppointmentTool(OperationalTool):
    tool_name = "manage_appointment"
    description = "Look up, reschedule, or cancel an existing appointment using deterministic persisted records."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "action": {"type": "string", "enum": ["lookup", "reschedule", "cancel"]},
        "phone": {"type": "string"}, "appointment_id": {"type": "string"},
        "slot_token": {"type": "string"}}, "required": ["action"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            action = str(parameters["action"])
            if action == "lookup":
                phone = str(parameters.get("phone") or context.caller_number or "")
                records = await service.find_appointments(org, phone)
                return {"status": "success", "data": {"appointments": records},
                        "message": "Existing appointments returned." if records else "No confirmed appointment was found."}
            appointment_id = str(parameters.get("appointment_id") or "").strip()
            if not appointment_id:
                raise ValueError("appointment_id is required")
            result = await service.change_appointment(org, context.call_id, appointment_id, action,
                                                      str(parameters.get("slot_token") or ""))
            await self._update_state(context, appointment_id=appointment_id,
                                     booking_state="cancelled" if action == "cancel" else "rescheduled")
            return {"status": "success", "data": result,
                    "message": "Appointment cancelled." if action == "cancel" else "Appointment rescheduled."}
        except Exception as exc:
            return self._error(exc)


class FinalizeReceptionistCallTool(OperationalTool):
    tool_name = "finalize_receptionist_call"
    description = "Save verified facts, summary, technician brief, languages, and outcome before ending the call."
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "verified_facts": {"type": "object"}, "ai_summary": {"type": "string"},
        "technician_brief": {"type": "string"}, "languages": {"type": "array", "items": {"type": "string"}},
        "outcome": {"type": "string", "enum": ["booked", "callback", "escalated", "information_only", "abandoned", "failed"]}},
        "required": ["verified_facts", "ai_summary", "technician_brief", "languages", "outcome"]}

    async def execute(self, parameters: Dict[str, Any], context: ToolExecutionContext) -> Dict[str, Any]:
        try:
            _, org, service = self._parts(context)
            session = await context.get_session()
            payload = dict(parameters); payload["transcript"] = list(getattr(session, "conversation_history", []) or [])
            result = await service.save_artifacts(org, context.call_id, payload)
            await self._update_state(context, summary_state="saved")
            return {"status": "success", "data": result, "message": "Call outcome and operational brief saved."}
        except Exception as exc:
            return self._error(exc)


OPERATIONAL_TOOL_CLASSES = [ClassifyIntentTool, IdentifyCustomerTool, CreateCustomerTool,
                            CreateLeadTool, GetServiceCatalogTool, CheckSafetyRulesTool,
                            CheckServiceAreaTool, GetAvailableSlotsTool, BookAppointmentTool,
                            ManageAppointmentTool, CreateCallbackTool, FinalizeReceptionistCallTool]
