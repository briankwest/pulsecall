"""Shared helpers between outbound (PollingAgent) and inbound (InboundAgent)."""
from typing import Any

from signalwire.core.function_result import SwaigFunctionResult

import config
import db
import events


DNC_PHRASES = [
    "don't call me",
    "stop calling",
    "take me off your list",
    "remove me from your list",
    "don't contact me",
    "don't ever call",
    "i don't want these calls",
    "never call me",
    "do not call",
]


ANSWER_TYPE_HINTS = {
    "yesno": "Submit exactly YES or NO.",
    "multi": "Submit one of the choices above, matching exactly.",
    "scale": "Submit an integer 1-5 (1=strongly disagree, 5=strongly agree).",
    "open": "Capture the respondent's full answer verbatim as text.",
}


def question_for_ai(q: dict) -> dict:
    """Flatten a question row for consumption via global_data template expansion."""
    choices_text = ", ".join(q["choices"]) if q.get("choices") else ""
    return {
        "id": q["id"],
        "ordinal": q["ordinal"],
        "prompt": q["prompt_text"],
        "type": q["answer_type"],
        "choices_text": choices_text,
        "type_hint": ANSWER_TYPE_HINTS.get(q["answer_type"], ""),
        "confirm": bool(q.get("confirm")),
    }


# ----------------------------------------------------------------------
# Step builders — attached to an existing context.
# ----------------------------------------------------------------------

def build_ask_question_step(ctx) -> None:
    """Add the shared ask_question step that loops through questions."""
    step = ctx.add_step("ask_question")
    step.add_section("Task", "Ask the current poll question and record the answer.")
    step.add_bullets("Process", [
        "Ask exactly this question: ${global_data.current_question.prompt}",
        "Answer format: ${global_data.current_question.type_hint}",
        "For multi-choice questions the valid answers are: ${global_data.current_question.choices_text}",
        "Do NOT invent or paraphrase choices — the stored value must match one of those exactly.",
        "If ${global_data.current_question.confirm} is true, read the answer back to the "
        "respondent before submitting so they can correct a mishearing.",
        "Once you have the answer, call record_answer with the canonical value.",
        "If they refuse to answer THIS question (but want to continue), call skip_question.",
        "If they ask to be removed from the list, call mark_dnc instead.",
    ])
    step.set_step_criteria("One answer has been recorded (or skipped).")
    step.set_functions(["record_answer", "skip_question", "mark_dnc"])
    step.set_valid_steps(["ask_question", "wrap_up_completed", "wrap_up_dnc"])


def build_wrap_up_steps(ctx) -> None:
    """Add the three terminal wrap-up steps."""
    completed = ctx.add_step("wrap_up_completed")
    completed.set_text(
        "Say: 'That's all my questions. Thank you so much for participating in the "
        "${global_data.campaign.name} poll, ${global_data.voter.first_name}. "
        "Have a great day.' Then end the call.")
    completed.set_functions("none")
    completed.set_valid_steps([])

    declined = ctx.add_step("wrap_up_declined")
    declined.set_text(
        "Say: 'No problem at all. Thanks for your time, "
        "${global_data.voter.first_name}. Have a great day.' Then end the call.")
    declined.set_functions("none")
    declined.set_valid_steps([])

    dnc = ctx.add_step("wrap_up_dnc")
    dnc.set_text(
        "Say: 'Understood. I've removed your number from our list. "
        "You won't be called again. Goodbye.' Then end the call.")
    dnc.set_functions("none")
    dnc.set_valid_steps([])


# ----------------------------------------------------------------------
# Tool registration — called from each agent's __init__.
# ----------------------------------------------------------------------

def register_shared_tools(agent) -> None:
    """Register record_answer, skip_question, and mark_dnc on the agent."""

    @agent.tool(
        name="record_answer",
        description="Record the respondent's answer to the current poll question.",
        wait_file="/sounds/typing.mp3",
        fillers={"en-US": ["Got it.", "Recording your answer."]},
        parameters={
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "The canonical answer. For yesno: YES or NO. "
                                   "For multi: one of the configured choices (match exactly). "
                                   "For scale: '1' through '5'. For open: the full answer text.",
                },
            },
            "required": ["value"],
        },
    )
    def record_answer(args: dict, raw_data: dict) -> SwaigFunctionResult:
        return persist_answer(args.get("value", ""), raw_data, skipped=False)

    @agent.tool(
        name="skip_question",
        description="Skip the current question because the respondent refused to answer it.",
        wait_file="/sounds/typing.mp3",
        fillers={"en-US": ["No problem, moving on."]},
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def skip_question(args: dict, raw_data: dict) -> SwaigFunctionResult:
        return persist_answer("SKIPPED", raw_data, skipped=True)

    @agent.tool(
        name="mark_dnc",
        description="Mark the caller as do-not-call. Use when respondent asks to be removed.",
        wait_file="/sounds/typing.mp3",
        fillers={"en-US": ["Removing you from the list right now."]},
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason (e.g. 'user_request').",
                },
            },
            "required": [],
        },
    )
    def mark_dnc(args: dict, raw_data: dict) -> SwaigFunctionResult:
        call_id = raw_data.get("call_id", "unknown")
        gd = raw_data.get("global_data", {}) or {}
        voter = gd.get("voter") or {}
        phone = voter.get("phone") or ""
        voter_id = int(voter.get("id") or 0)
        campaign_id = int((gd.get("campaign") or {}).get("id") or 0)
        reason = args.get("reason") or "user_request"

        if phone:
            db.add_dnc(phone, reason=reason, source_call=call_id)
        if voter_id and campaign_id:
            db.set_voter_state(campaign_id, voter_id, "dnc", call_id=call_id)

        result = SwaigFunctionResult(
            "Confirm politely that they've been removed, then end the call.")
        if phone and config.SIGNALWIRE_PHONE_NUMBER:
            result.send_sms(
                to_number=phone,
                from_number=config.SIGNALWIRE_PHONE_NUMBER,
                body=("You've been removed from our polling list. "
                      "You will not receive further calls. — PulseCall"),
            )
        result.swml_change_step("wrap_up_dnc")
        events.publish(f"call:{call_id}", "dnc", {"phone": phone, "reason": reason})
        if campaign_id:
            events.publish(f"campaign:{campaign_id}", "voter_dnc",
                           {"voter_id": voter_id, "phone": phone})
        return result


# ----------------------------------------------------------------------
# Core answer-recording logic
# ----------------------------------------------------------------------

def persist_answer(value: str, raw_data: dict, skipped: bool) -> SwaigFunctionResult:
    call_id = raw_data.get("call_id", "unknown")
    gd = raw_data.get("global_data", {}) or {}
    campaign = gd.get("campaign") or {}
    campaign_id = int(campaign.get("id") or 0)
    questions = gd.get("questions") or []
    idx = int(gd.get("current_index", 0) or 0)

    if idx < 0 or idx >= len(questions):
        result = SwaigFunctionResult("No current question — wrapping up.")
        result.swml_change_step("wrap_up_completed")
        return result

    q = questions[idx]
    value = (value or "").strip()

    # Belt-and-suspenders DNC detection — if the "answer" is actually a request
    # to be removed, treat it as mark_dnc would.
    if any(p in value.lower() for p in DNC_PHRASES):
        voter = gd.get("voter") or {}
        phone = voter.get("phone") or ""
        voter_id = int(voter.get("id") or 0)
        if phone:
            db.add_dnc(phone, reason="user_request", source_call=call_id)
        if voter_id and campaign_id:
            db.set_voter_state(campaign_id, voter_id, "dnc", call_id=call_id)
        result = SwaigFunctionResult(
            "The respondent asked to be removed. Confirm politely and end the call.")
        result.swml_change_step("wrap_up_dnc")
        events.publish(f"call:{call_id}", "dnc", {"phone": phone, "reason": "detected_in_answer"})
        return result

    db.insert_answer(call_id, q["id"], value)
    answered_count = int(gd.get("answered_count", 0) or 0) + 1

    events.publish(f"call:{call_id}", "answer", {
        "question_id": q["id"],
        "ordinal": q["ordinal"],
        "prompt": q["prompt_text"],
        "value": value,
        "skipped": skipped,
    })
    if campaign_id:
        events.publish(f"campaign:{campaign_id}", "progress", {
            "call_id": call_id,
            "answered_count": answered_count,
        })

    next_idx = idx + 1
    if next_idx >= len(questions):
        result = SwaigFunctionResult(
            "Thank them and end the call — that was the last question.")
        new_gd = dict(gd)
        new_gd["current_index"] = next_idx
        new_gd["answered_count"] = answered_count
        result.update_global_data(new_gd)
        result.swml_change_step("wrap_up_completed")
        return result

    next_q = questions[next_idx]
    new_gd = dict(gd)
    new_gd["current_index"] = next_idx
    new_gd["current_question"] = question_for_ai(next_q)
    new_gd["answered_count"] = answered_count

    ack = "Got it." if not skipped else "No problem, skipping."
    result = SwaigFunctionResult(f"{ack} Next question: {next_q['prompt_text']}")
    result.update_global_data(new_gd)
    result.swml_change_step("ask_question")
    return result


def start_poll_result(gd: dict) -> SwaigFunctionResult:
    """Given a global_data with loaded questions, produce the result that kicks
    off the ask_question loop. Used by both give_consent (outbound) and
    accept_poll_offer (inbound)."""
    questions = gd.get("questions") or []
    if not questions:
        result = SwaigFunctionResult(
            "No questions configured for this campaign — thank them and end the call.")
        result.swml_change_step("wrap_up_completed")
        return result

    q = questions[0]
    new_gd = dict(gd)
    new_gd["current_index"] = 0
    new_gd["current_question"] = question_for_ai(q)
    new_gd["answered_count"] = 0

    result = SwaigFunctionResult(
        f"Great — starting the poll. First question: {q['prompt_text']}")
    result.update_global_data(new_gd)
    result.swml_change_step("ask_question")
    return result
