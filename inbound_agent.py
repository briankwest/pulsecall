"""PulseCall inbound agent — handles callbacks to our SignalWire number.

Behavior by caller state (looked up by caller_id_number):
    - In DNC list               → confirm removal, offer re-opt-in, goodbye.
    - Known voter w/ pending    → offer to take the poll now.
    - Known voter completed     → thank them, offer opt-out.
    - Unknown caller            → explain who we are, offer opt-out.

The caller can ALWAYS say "don't call me" and we'll honor it.
"""
import json
import logging
from pathlib import Path
from typing import Optional

from signalwire import AgentBase
from signalwire.core.function_result import SwaigFunctionResult

import config
import db
import events
import agent_shared

logger = logging.getLogger("pulsecall.inbound")


class InboundAgent(AgentBase):
    """Mounted at /inbound — this is the phone number's Call Handler URL."""

    def __init__(self) -> None:
        super().__init__(
            name="PulseCallInbound",
            route="/inbound",
            record_call=True,
            record_format="wav",
            record_stereo=True,
        )

        self.set_param("ai_model", config.AI_MODEL)
        self.set_prompt_llm_params(top_p=config.AI_TOP_P, temperature=config.AI_TEMPERATURE)

        self.prompt_add_section(
            "Personality",
            "You are a polite, neutral political-polling service. "
            "You keep every response to one or two short sentences — this is a phone call. "
            "You are UP-FRONT about being a polling service; you never pretend otherwise."
        )
        self.prompt_add_section(
            "Rules",
            bullets=[
                "This is a PHONE CALL. Keep every response to 1-2 short sentences.",
                "Be up-front: identify yourself as PulseCall and your purpose (polling).",
                "If the caller says ANY of these phrases, call mark_dnc immediately: "
                "'don't call me', 'stop calling', 'take me off your list', 'remove me', "
                "'don't contact me', 'do not call', 'never call me'.",
                "Do not argue, debate, or express political opinions.",
            ],
        )

        self.add_language("English", "en-US", "azure.en-US-AvaNeural")
        self.add_hints([
            "PulseCall", "poll", "polling", "survey",
            "remove me", "opt out", "take me off",
            "don't call me", "stop calling", "do not call",
        ])
        self.set_post_prompt(
            "Summarize the inbound call in 1-2 sentences. Note whether the caller took "
            "the poll, opted out (DNC), re-opted-in, or just asked a question.")

        self._define_state_machine()
        self._define_tools()
        agent_shared.register_shared_tools(self)
        self.set_dynamic_config_callback(self._per_call_config)

    # ------------------------------------------------------------------
    def _define_state_machine(self) -> None:
        contexts = self.define_contexts()
        ctx = contexts.add_context("default")

        greet = ctx.add_step("greet_inbound")
        greet.add_section("Task",
            "Greet the caller honestly and route them based on their status.")
        greet.add_bullets("Process", [
            "Start by saying: 'Thanks for calling PulseCall — we run short public-opinion polls.'",
            "Caller status (check global_data.caller_status):",
            "  - 'dnc':      Say: 'I see your number is on our do-not-call list. "
            "You won't be called. Would you like to stay on the list?' If they say "
            "yes or are unsure, call wrap_dnc_confirmed. If they explicitly say they "
            "want to receive calls again, call remove_from_dnc.",
            "  - 'pending_voter': Say: 'I have you on the list for our "
            "${global_data.campaign.name} poll — would you like to take it now? "
            "It takes about two minutes.' If yes, call accept_poll_offer(accepted=true). "
            "If no, call accept_poll_offer(accepted=false).",
            "  - 'completed_voter': Say: 'Thanks — our records show you already took "
            "the ${global_data.campaign.name} poll. Is there anything else I can help "
            "with?' Offer mark_dnc if they want off the list.",
            "  - 'unknown': Say: 'Thanks for calling. We don't have your number in any "
            "active poll right now. You can ask to be added to our do-not-call list — "
            "just say the word.' Offer mark_dnc.",
            "At any point, if they ask to be removed, call mark_dnc.",
        ])
        greet.set_step_criteria(
            "Caller has either started the poll, accepted DNC, removed DNC, or wrapped up.")
        greet.set_functions([
            "accept_poll_offer", "mark_dnc", "remove_from_dnc", "wrap_dnc_confirmed"
        ])
        greet.set_valid_steps([
            "ask_question", "wrap_up_declined", "wrap_up_dnc",
            "wrap_up_dnc_confirmed", "wrap_up_reopt",
        ])

        agent_shared.build_ask_question_step(ctx)
        agent_shared.build_wrap_up_steps(ctx)

        # Inbound-specific terminal steps.
        confirmed = ctx.add_step("wrap_up_dnc_confirmed")
        confirmed.set_text(
            "Say: 'Great — you'll stay on our do-not-call list. "
            "Thanks for calling.' Then end the call.")
        confirmed.set_functions("none")
        confirmed.set_valid_steps([])

        reopt = ctx.add_step("wrap_up_reopt")
        reopt.set_text(
            "Say: 'You've been removed from the do-not-call list. "
            "You may receive future polling calls. Thanks for reaching out.' "
            "Then end the call.")
        reopt.set_functions("none")
        reopt.set_valid_steps([])

    # ------------------------------------------------------------------
    def _define_tools(self) -> None:

        @self.tool(
            name="accept_poll_offer",
            description="Respondent has been offered a poll — record their decision.",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["One moment."]},
            parameters={
                "type": "object",
                "properties": {
                    "accepted": {
                        "type": "boolean",
                        "description": "True if they want to take the poll now, false if not.",
                    },
                },
                "required": ["accepted"],
            },
        )
        def accept_poll_offer(args: dict, raw_data: dict) -> SwaigFunctionResult:
            call_id = raw_data.get("call_id", "unknown")
            gd = raw_data.get("global_data", {}) or {}
            if not bool(args.get("accepted")):
                result = SwaigFunctionResult(
                    "Thank them politely and end the call.")
                result.swml_change_step("wrap_up_declined")
                events.publish(f"call:{call_id}", "declined", {"call_id": call_id})
                return result

            events.publish(f"call:{call_id}", "consented", {"call_id": call_id})
            return agent_shared.start_poll_result(gd)

        @self.tool(
            name="remove_from_dnc",
            description="Caller is currently on the DNC list and asked to be removed FROM the DNC list (they want to receive calls again).",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Adjusting your preferences."]},
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def remove_from_dnc(args: dict, raw_data: dict) -> SwaigFunctionResult:
            call_id = raw_data.get("call_id", "unknown")
            gd = raw_data.get("global_data", {}) or {}
            phone = (gd.get("voter") or {}).get("phone") or gd.get("caller_phone") or ""
            if phone:
                db.remove_dnc(phone)
                logger.info("Inbound caller %s removed from DNC", phone)
            result = SwaigFunctionResult(
                "Confirm they've been removed from the DNC list and end the call.")
            result.swml_change_step("wrap_up_reopt")
            events.publish(f"call:{call_id}", "reopt", {"phone": phone})
            return result

        @self.tool(
            name="wrap_dnc_confirmed",
            description="Caller confirmed they want to stay on the DNC list. Wrap up.",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["No problem."]},
            parameters={"type": "object", "properties": {}, "required": []},
        )
        def wrap_dnc_confirmed(args: dict, raw_data: dict) -> SwaigFunctionResult:
            result = SwaigFunctionResult(
                "Confirm they'll stay on the list and end the call.")
            result.swml_change_step("wrap_up_dnc_confirmed")
            return result

    # ------------------------------------------------------------------
    def _per_call_config(self, query_params: dict, body_params: dict,
                         headers: dict, agent: "InboundAgent") -> None:
        body = body_params or {}
        call = body.get("call") or {}
        caller_phone = (call.get("caller_id_number") or body.get("caller_id_number")
                        or call.get("from") or "")
        call_id = (body.get("call_id") or call.get("call_id")
                   or (query_params or {}).get("call_id") or "unknown")

        status = "unknown"
        voter_row = None
        campaign = None
        questions: list[dict] = []

        if caller_phone and db.is_dnc(caller_phone):
            status = "dnc"
        elif caller_phone:
            voter_row = db.find_voter_by_phone(caller_phone)
            if voter_row and voter_row.get("campaign_id"):
                campaign = db.get_campaign(voter_row["campaign_id"])
                questions = db.get_questions(voter_row["campaign_id"])
                state_status = voter_row.get("state_status")
                if state_status == "completed":
                    status = "completed_voter"
                elif state_status in ("pending", "failed"):
                    status = "pending_voter"
                elif state_status == "dnc":
                    status = "dnc"
                else:
                    status = "completed_voter"
            elif voter_row:
                # Voter exists in some list but has no campaign state yet.
                status = "unknown"

        if status == "pending_voter" and call_id != "unknown" and campaign:
            db.create_call(call_id=call_id, campaign_id=campaign["id"],
                           voter_id=voter_row["id"])

        gd = {
            "caller_status": status,
            "caller_phone": caller_phone,
            "voter": {
                "id": (voter_row or {}).get("id", 0),
                "first_name": (voter_row or {}).get("first_name") or "there",
                "phone": caller_phone,
            },
            "campaign": {
                "id": (campaign or {}).get("id", 0),
                "name": (campaign or {}).get("name", "PulseCall"),
                "script_intro": (campaign or {}).get("script_intro", ""),
            },
            "questions": [
                {
                    "id": q["id"],
                    "ordinal": q["ordinal"],
                    "prompt_text": q["prompt_text"],
                    "answer_type": q["answer_type"],
                    "choices": q["choices"],
                    "confirm": q["confirm"],
                } for q in questions
            ],
            "current_index": -1,
            "current_question": {"prompt": "", "type": "", "choices_text": "",
                                 "type_hint": "", "confirm": False},
            "answered_count": 0,
        }
        agent.update_global_data(gd)
        logger.info("inbound call %s from %s status=%s", call_id, caller_phone, status)

    # ------------------------------------------------------------------
    def on_summary(self, summary: Optional[dict] = None,
                   raw_data: Optional[dict] = None) -> None:
        raw_data = raw_data or {}
        call_id = raw_data.get("call_id", "unknown")

        # Close any call we opened (pending_voter path).
        call = db.get_call(call_id) if call_id != "unknown" else None
        if call and not call.get("ended_at"):
            answers = db.answers_for_call(call_id)
            outcome = "completed" if answers else "inbound"
            summary_text = None
            if isinstance(summary, dict):
                summary_text = summary.get("content") or summary.get("summary")
            elif summary:
                summary_text = str(summary)
            db.end_call(call_id, outcome=outcome, summary=summary_text)
            if answers:
                db.set_voter_state(call["campaign_id"], call["voter_id"],
                                   "completed", call_id=call_id)
            events.publish(f"call:{call_id}", "ended",
                           {"outcome": outcome, "summary": summary_text})
            # Kick the outbound dialer so the campaign wraps immediately if
            # this inbound callback drained the last pending voter.
            from dialer import dialer
            dialer.notify_call_ended(call["campaign_id"])

        calls_dir = Path(__file__).parent / "calls"
        calls_dir.mkdir(exist_ok=True)
        try:
            (calls_dir / f"{call_id}.json").write_text(
                json.dumps(raw_data, indent=2, default=str))
        except Exception:
            logger.exception("failed to write call archive for %s", call_id)
