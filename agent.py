"""PulseCall outbound polling agent — data-driven by DB state."""
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

logger = logging.getLogger("pulsecall.agent")


class PollingAgent(AgentBase):
    """Outbound polling agent mounted at /outbound.

    Flow:
        greeting           → consent (tool)
        consent: yes       → ask_question
        consent: no        → wrap_up_declined
        consent: opt-out   → wrap_up_dnc
        ask_question loop  → wrap_up_completed
    """

    def __init__(self) -> None:
        super().__init__(
            name="PulseCallOutbound",
            route="/outbound",
            record_call=True,
            record_format="wav",
            record_stereo=True,
        )

        self.set_param("ai_model", config.AI_MODEL)
        self.set_prompt_llm_params(top_p=config.AI_TOP_P, temperature=config.AI_TEMPERATURE)

        self.prompt_add_section(
            "Personality",
            "You are a polite, neutral political-polling interviewer. You do not offer opinions. "
            "Keep every response to one or two short sentences — this is a phone call."
        )
        self.prompt_add_section(
            "Rules",
            bullets=[
                "This is a PHONE CALL. Keep every response to 1-2 short sentences.",
                "Do not argue with, debate, or correct the respondent.",
                "Stay neutral. Never express a political opinion.",
                "If the respondent says ANY of these, immediately call mark_dnc and say "
                "goodbye politely — do not try to convince them: "
                "'don't call me', 'stop calling', 'take me off your list', "
                "'remove me', 'don't contact me', 'do not call', 'never call me'.",
                "Refer to the campaign as ${global_data.campaign.name}.",
                "Speak naturally: say percentages as words ('forty percent', not '40%').",
            ],
        )

        self.add_language("English", "en-US", "azure.en-US-AvaNeural")
        self.add_hints([
            "poll", "polling", "survey",
            "Democrat", "Republican", "Independent",
            "approve", "disapprove", "undecided",
            "strongly approve", "somewhat approve", "strongly disapprove",
            "right direction", "wrong track",
            "don't call me", "stop calling", "take me off your list",
            "remove me", "do not call",
        ])
        self.set_post_prompt(
            "Summarize the conversation in 1-2 sentences. Note whether the respondent "
            "completed the poll, declined, or asked to be removed (DNC).")

        self._define_state_machine()
        self._define_tools()
        agent_shared.register_shared_tools(self)
        self.set_dynamic_config_callback(self._per_call_config)

    # ------------------------------------------------------------------
    def _define_state_machine(self) -> None:
        contexts = self.define_contexts()
        ctx = contexts.add_context("default")

        greeting = ctx.add_step("greeting")
        greeting.add_section("Task",
            "Greet the respondent by first name and introduce the poll.")
        greeting.add_bullets("Process", [
            "Say exactly: 'Hello ${global_data.voter.first_name}, this is a quick "
            "political poll from ${global_data.campaign.name}.'",
            "Then read the script intro: ${global_data.campaign.script_intro}",
            "Ask: 'Do you have a couple of minutes to participate?'",
            "When they answer, call give_consent with consented=true or consented=false.",
            "If at any point they say any phrase about not being called, "
            "call mark_dnc instead.",
        ])
        greeting.set_step_criteria("Respondent consented, declined, or asked to be removed.")
        greeting.set_functions(["give_consent", "mark_dnc"])
        greeting.set_valid_steps(["ask_question", "wrap_up_declined", "wrap_up_dnc"])

        agent_shared.build_ask_question_step(ctx)
        agent_shared.build_wrap_up_steps(ctx)

    # ------------------------------------------------------------------
    def _define_tools(self) -> None:
        @self.tool(
            name="give_consent",
            description="Record whether the respondent consented to take the poll.",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["One moment."]},
            parameters={
                "type": "object",
                "properties": {
                    "consented": {
                        "type": "boolean",
                        "description": "True if they agreed, false if they declined.",
                    },
                },
                "required": ["consented"],
            },
        )
        def give_consent(args: dict, raw_data: dict) -> SwaigFunctionResult:
            consented = bool(args.get("consented"))
            call_id = raw_data.get("call_id", "unknown")
            gd = raw_data.get("global_data", {}) or {}
            campaign_id = int((gd.get("campaign") or {}).get("id") or 0)

            if not consented:
                result = SwaigFunctionResult(
                    "Thank them politely and end the call. Do not argue.")
                result.swml_change_step("wrap_up_declined")
                events.publish(f"call:{call_id}", "declined", {"call_id": call_id})
                if campaign_id:
                    events.publish(f"campaign:{campaign_id}", "voter_declined",
                                   {"call_id": call_id})
                return result

            events.publish(f"call:{call_id}", "consented", {"call_id": call_id})
            return agent_shared.start_poll_result(gd)

    # ------------------------------------------------------------------
    def _per_call_config(self, query_params: dict, body_params: dict,
                         headers: dict, agent: "PollingAgent") -> None:
        params = {**(query_params or {}), **(body_params or {})}

        try:
            campaign_id = int(params.get("campaign_id") or 0)
            voter_id = int(params.get("voter_id") or 0)
        except (TypeError, ValueError):
            campaign_id = voter_id = 0

        call_id = (body_params or {}).get("call_id") \
            or ((body_params or {}).get("call") or {}).get("call_id") \
            or params.get("call_id") \
            or "unknown"

        if not campaign_id or not voter_id:
            # Anonymous hit on /outbound — almost always a mis-routed inbound
            # call whose number's Voice webhook is pointed at /outbound instead
            # of /inbound. The default greeting step would happily pretend to
            # start a poll and immediately wrap up (no questions loaded); give
            # a honest fallback that offers DNC and ends the call.
            caller_phone = (
                (body_params or {}).get("caller_id_number")
                or ((body_params or {}).get("call") or {}).get("caller_id_number")
                or ""
            )
            logger.warning(
                "outbound hit without campaign/voter params (caller=%s) — "
                "check that the SignalWire phone number's Voice URL points to "
                "/inbound, not /outbound", caller_phone)
            ctx = agent._contexts_builder.get_context("default")
            step = ctx.get_step("greeting") if ctx else None
            if step:
                step.clear_sections()
                step.set_text(
                    "Say exactly: 'Hi, you've reached PulseCall polling "
                    "service. We don't have your number on any active poll "
                    "right now. If you'd like to be added to our do-not-call "
                    "list, just say remove me — otherwise, thanks for calling "
                    "and goodbye.' "
                    "If they say anything about not wanting calls, call mark_dnc. "
                    "Otherwise call give_consent with consented=false to end the call. "
                    "Do NOT ask any poll questions — there are none to ask."
                )
            agent.update_global_data({
                "campaign": {"id": 0, "name": "PulseCall", "script_intro": ""},
                "voter": {"id": 0, "first_name": "there", "phone": caller_phone},
                "questions": [],
            })
            return

        campaign = db.get_campaign(campaign_id)
        voter = db.get_voter(voter_id)
        questions = db.get_questions(campaign_id)

        if not campaign or not voter:
            logger.warning("Missing campaign %s or voter %s for call %s",
                           campaign_id, voter_id, call_id)
            return

        # Final DNC check — a voter may have been opted out between dial and answer.
        if db.is_dnc(voter["phone"]):
            logger.info("Call %s targets DNC number %s — short-circuiting",
                        call_id, voter["phone"])
            agent.update_global_data({
                "campaign": {"id": campaign["id"], "name": campaign["name"],
                             "script_intro": campaign["script_intro"]},
                "voter": {"id": voter["id"],
                          "first_name": voter.get("first_name") or "there",
                          "phone": voter["phone"]},
                "questions": [],
            })
            return

        if call_id and call_id != "unknown":
            db.create_call(call_id=call_id, campaign_id=campaign_id, voter_id=voter_id)
            db.set_voter_state(campaign_id, voter_id, "calling", call_id=call_id)

        agent.update_global_data({
            "campaign": {
                "id": campaign["id"],
                "name": campaign["name"],
                "script_intro": campaign["script_intro"],
            },
            "voter": {
                "id": voter["id"],
                "first_name": voter.get("first_name") or "there",
                "phone": voter["phone"],
            },
            "questions": [
                {
                    "id": q["id"],
                    "ordinal": q["ordinal"],
                    "prompt_text": q["prompt_text"],
                    "answer_type": q["answer_type"],
                    "choices": q["choices"],
                    "confirm": q["confirm"],
                }
                for q in questions
            ],
            "current_index": -1,
            "current_question": {"prompt": "", "type": "", "choices_text": "",
                                 "type_hint": "", "confirm": False},
            "answered_count": 0,
        })

    # ------------------------------------------------------------------
    def on_summary(self, summary: Optional[dict] = None,
                   raw_data: Optional[dict] = None) -> None:
        raw_data = raw_data or {}
        call_id = raw_data.get("call_id", "unknown")
        summary_text = None
        if isinstance(summary, dict):
            summary_text = summary.get("content") or summary.get("summary") \
                or json.dumps(summary)
        elif summary:
            summary_text = str(summary)

        call = db.get_call(call_id) if call_id != "unknown" else None
        if call and not call.get("ended_at"):
            state = db.get_voter_state(call["campaign_id"], call["voter_id"])
            outcome = "completed"
            if state:
                if state["status"] == "dnc":
                    outcome = "dnc"
                elif state["status"] == "calling":
                    answers = db.answers_for_call(call_id)
                    outcome = "completed" if answers else "no_answer"
                    db.set_voter_state(
                        call["campaign_id"], call["voter_id"],
                        "completed" if answers else "failed",
                        call_id=call_id,
                    )
            db.end_call(call_id, outcome=outcome, summary=summary_text)
            events.publish(f"call:{call_id}", "ended",
                           {"outcome": outcome, "summary": summary_text})
            events.publish(f"campaign:{call['campaign_id']}", "call_ended",
                           {"call_id": call_id, "outcome": outcome})
            # Kick the dialer so it either dials the next voter or transitions
            # the campaign to 'completed' immediately — don't wait 30s for
            # the next scheduler tick.
            from dialer import dialer
            dialer.notify_call_ended(call["campaign_id"])

        calls_dir = Path(__file__).parent / "calls"
        calls_dir.mkdir(exist_ok=True)
        try:
            (calls_dir / f"{call_id}.json").write_text(
                json.dumps(raw_data, indent=2, default=str))
        except Exception:
            logger.exception("failed to write call archive for %s", call_id)
