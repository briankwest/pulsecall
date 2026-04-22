"""PulseCall HTTP server — mounts the polling agent plus REST + SSE endpoints."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from signalwire import AgentServer

import config
import db
import events
from agent import PollingAgent
from inbound_agent import InboundAgent
from dialer import dialer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("pulsecall.server")


def _install_lifespan(app: FastAPI) -> None:
    """Wire PulseCall's startup/shutdown work into an ASGI lifespan context
    manager, chaining any handlers the SDK registered via its own on_event
    hooks (e.g. the catch-all route setup) so they still run.

    This replaces FastAPI's deprecated @app.on_event decorators.
    """
    existing_startup = list(app.router.on_startup)
    existing_shutdown = list(app.router.on_shutdown)

    async def _run(handlers: list) -> None:
        for h in handlers:
            result = h()
            if asyncio.iscoroutine(result):
                await result

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # SDK startup first (registers the catch-all route that serves
        # /outbound and /inbound without trailing slash).
        await _run(existing_startup)

        events.set_loop(asyncio.get_running_loop())
        dialer.configure_cron()
        dialer.recover_running_campaigns()
        logger.info("PulseCall started — HOST=%s PORT=%s", config.HOST, config.PORT)

        try:
            yield
        finally:
            dialer.shutdown()
            await _run(existing_shutdown)

    app.router.lifespan_context = lifespan
    # Drain the on_startup/on_shutdown lists so Starlette's default lifespan
    # doesn't re-invoke the SDK handlers on top of ours.
    app.router.on_startup = []
    app.router.on_shutdown = []


def create_server() -> AgentServer:
    db.init_db()

    server = AgentServer(host=config.HOST, port=config.PORT)
    server.register(PollingAgent(), "/outbound")
    server.register(InboundAgent(), "/inbound")

    app = server.app
    _install_lifespan(app)

    # ------------------------------------------------------------------
    # Metadata for the dashboard
    # ------------------------------------------------------------------
    @app.get("/api/phone")
    def get_phone() -> dict:
        return {
            "phone": config.SIGNALWIRE_PHONE_NUMBER,
            "display": config.DISPLAY_PHONE_NUMBER or config.SIGNALWIRE_PHONE_NUMBER,
        }

    # ------------------------------------------------------------------
    # Campaigns
    # ------------------------------------------------------------------
    @app.get("/api/campaigns")
    def list_campaigns() -> dict:
        return {"campaigns": db.list_campaigns()}

    @app.post("/api/campaigns")
    async def create_campaign(req: Request) -> dict:
        body = await req.json()
        name = (body.get("name") or "").strip()
        script_intro = (body.get("script_intro") or "").strip()
        if not name or not script_intro:
            raise HTTPException(400, "name and script_intro required")
        caller_id = (body.get("caller_id") or "").strip() or config.SIGNALWIRE_PHONE_NUMBER or None
        list_ids = [int(x) for x in (body.get("list_ids") or []) if str(x).isdigit()]
        campaign_id = db.create_campaign(
            name=name,
            script_intro=script_intro,
            caller_id=caller_id,
            max_concurrent=int(body.get("max_concurrent") or 2),
            list_ids=list_ids,
        )
        for i, q in enumerate(body.get("questions") or [], start=1):
            db.add_question(
                campaign_id=campaign_id,
                ordinal=i,
                prompt_text=q["prompt_text"],
                answer_type=q["answer_type"],
                choices=q.get("choices"),
                confirm=bool(q.get("confirm")),
            )
        return {"ok": True, "campaign_id": campaign_id}

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: int) -> dict:
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(404, "campaign not found")
        return {
            "campaign": campaign,
            "questions": db.get_questions(campaign_id),
            "lists": db.get_campaign_lists(campaign_id),
            "voters": db.list_campaign_voters(campaign_id),
            "results": db.campaign_results(campaign_id),
        }

    @app.patch("/api/campaigns/{campaign_id}")
    async def patch_campaign(campaign_id: int, req: Request) -> dict:
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(404, "campaign not found")
        body = await req.json()

        # Always-editable metadata.
        db.update_campaign(
            campaign_id,
            name=body.get("name"),
            script_intro=body.get("script_intro"),
            caller_id=body.get("caller_id"),
            max_concurrent=int(body["max_concurrent"]) if body.get("max_concurrent") is not None else None,
        )

        # Questions can only be edited while the campaign hasn't run.
        if "questions" in body:
            if campaign["status"] != "draft":
                raise HTTPException(
                    409,
                    "Questions can only be edited while the campaign is in 'draft' status. "
                    "Pause and create a new campaign to change questions after launch.")
            db.replace_questions(campaign_id, body["questions"])

        # Lists (campaign ↔ list links) editable any time.
        if "list_ids" in body:
            current = {l["id"] for l in db.get_campaign_lists(campaign_id)}
            desired = {int(x) for x in (body["list_ids"] or []) if str(x).isdigit()}
            for lid in desired - current:
                db.link_list(campaign_id, lid)
            for lid in current - desired:
                db.unlink_list(campaign_id, lid)

        return {"ok": True}

    @app.delete("/api/campaigns/{campaign_id}")
    def remove_campaign(campaign_id: int) -> dict:
        db.delete_campaign(campaign_id)
        return {"ok": True}

    @app.post("/api/campaigns/{campaign_id}/start")
    def start_campaign(campaign_id: int) -> dict:
        r = dialer.start_campaign(campaign_id)
        if not r.get("ok"):
            raise HTTPException(409, r.get("error", "start rejected"))
        return r

    @app.post("/api/campaigns/{campaign_id}/pause")
    def pause_campaign(campaign_id: int) -> dict:
        return dialer.pause_campaign(campaign_id)

    @app.post("/api/campaigns/{campaign_id}/retry-failed")
    def retry_failed(campaign_id: int) -> dict:
        """Flip 'failed' + stuck 'calling' rows back to 'pending', then start."""
        r = dialer.retry_failed(campaign_id)
        if not r.get("ok"):
            raise HTTPException(409, r.get("error", "retry rejected"))
        return r

    @app.post("/api/campaigns/{campaign_id}/reset")
    def reset_campaign(campaign_id: int) -> dict:
        """DESTRUCTIVE: reset all non-DNC voters to 'pending', wipe calls+answers.
        Meant for testing — the UI requires an explicit confirmation."""
        return dialer.reset_campaign(campaign_id)

    @app.get("/api/campaigns/{campaign_id}/results")
    def campaign_results(campaign_id: int) -> dict:
        return db.campaign_results(campaign_id)

    @app.get("/api/campaigns/{campaign_id}/export/answers.csv")
    def export_answers(campaign_id: int) -> Response:
        csv_text = db.export_answers_csv(campaign_id)
        campaign = db.get_campaign(campaign_id)
        fname = f"answers-{campaign_id}-{(campaign or {}).get('name','').replace(' ','_')}.csv"
        return Response(
            content=csv_text, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/campaigns/{campaign_id}/export/voters.csv")
    def export_voters(campaign_id: int) -> Response:
        csv_text = db.export_voters_csv(campaign_id)
        campaign = db.get_campaign(campaign_id)
        fname = f"voters-{campaign_id}-{(campaign or {}).get('name','').replace(' ','_')}.csv"
        return Response(
            content=csv_text, media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # ------------------------------------------------------------------
    # Voter lists
    # ------------------------------------------------------------------
    @app.get("/api/lists")
    def list_all_lists() -> dict:
        return {"lists": db.list_lists()}

    @app.post("/api/lists")
    async def create_voter_list(req: Request) -> dict:
        body = await req.json()
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        try:
            lid = db.create_list(name=name, description=body.get("description"))
        except Exception:
            raise HTTPException(409, f"list name '{name}' already exists")
        return {"ok": True, "list_id": lid}

    @app.get("/api/lists/{list_id}")
    def get_voter_list(list_id: int) -> dict:
        lst = db.get_list(list_id)
        if not lst:
            raise HTTPException(404, "list not found")
        return {
            "list": lst,
            "voters": db.list_voters_in_list(list_id),
            "campaigns": db.list_campaigns_using_list(list_id),
        }

    @app.patch("/api/lists/{list_id}")
    async def patch_voter_list(list_id: int, req: Request) -> dict:
        body = await req.json()
        db.update_list(list_id, name=body.get("name"),
                       description=body.get("description"))
        return {"ok": True}

    @app.delete("/api/lists/{list_id}")
    def delete_voter_list(list_id: int) -> dict:
        db.delete_list(list_id)
        return {"ok": True}

    @app.post("/api/lists/{list_id}/voters")
    async def add_voters_to_list(list_id: int, req: Request) -> dict:
        body = await req.json()
        voters = body.get("voters") or []
        # Accept CSV blob as an alternative to a JSON voters array.
        if not voters and isinstance(body.get("csv"), str):
            voters = db.parse_voter_csv(body["csv"])
        result = db.bulk_add_voters(list_id, voters)
        return {"ok": True, **result}

    # ------------------------------------------------------------------
    # Voter CRUD
    # ------------------------------------------------------------------
    @app.get("/api/voters/{voter_id}")
    def get_voter(voter_id: int) -> dict:
        voter = db.get_voter(voter_id)
        if not voter:
            raise HTTPException(404, "voter not found")
        return voter

    @app.patch("/api/voters/{voter_id}")
    async def patch_voter(voter_id: int, req: Request) -> dict:
        body = await req.json()
        db.update_voter(voter_id, **{k: body.get(k) for k in (
            "phone", "first_name", "last_name", "zip_code",
            "gender", "age_band", "party") if k in body})
        return {"ok": True}

    @app.delete("/api/voters/{voter_id}")
    def remove_voter(voter_id: int) -> dict:
        db.delete_voter(voter_id)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------
    @app.get("/api/calls/{call_id}")
    def get_call(call_id: str) -> dict:
        snap = db.get_call_snapshot(call_id)
        if not snap.get("found"):
            raise HTTPException(404, "call not found")
        return snap

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    @app.get("/api/reports/overview")
    def reports_overview() -> dict:
        return db.reports_overview()

    # ------------------------------------------------------------------
    # DNC
    # ------------------------------------------------------------------
    @app.get("/api/dnc")
    def list_dnc() -> dict:
        return {"dnc": db.list_dnc()}

    @app.post("/api/dnc")
    async def add_dnc(req: Request) -> dict:
        body = await req.json()
        phone = (body.get("phone") or "").strip()
        if not phone:
            raise HTTPException(400, "phone required")
        db.add_dnc(phone, reason=body.get("reason") or "manual")
        return {"ok": True}

    @app.post("/api/dnc/bulk")
    async def add_dnc_bulk(req: Request) -> dict:
        """Accept either {entries:[{phone, reason?}]} or {csv:"phone,reason\\n..."}."""
        body = await req.json()
        entries = body.get("entries") or []
        if not entries and isinstance(body.get("csv"), str):
            # Reuse the voter CSV parser (it only needs phone) — then tack on reason.
            rows = db.parse_voter_csv(body["csv"])
            # If the CSV had a "reason" column, parse_voter_csv would have
            # dropped it; do a second pass manually for reason support.
            import csv, io
            reader = csv.reader(io.StringIO(body["csv"].strip()))
            raw = list(reader)
            if raw:
                header = [h.strip().lower() for h in raw[0]]
                if "phone" in header:
                    i_phone = header.index("phone")
                    i_reason = header.index("reason") if "reason" in header else None
                    entries = []
                    for r in raw[1:]:
                        if i_phone >= len(r): continue
                        entries.append({
                            "phone": r[i_phone].strip(),
                            "reason": r[i_reason].strip() if i_reason is not None and i_reason < len(r) else "manual",
                        })
                else:
                    entries = [{"phone": r[0].strip(), "reason": "manual"}
                               for r in raw if r and r[0].strip()]
        result = db.bulk_add_dnc(entries)
        return {"ok": True, **result}

    @app.delete("/api/dnc/{phone}")
    def delete_dnc(phone: str) -> dict:
        db.remove_dnc(phone)
        return {"ok": True}

    # ------------------------------------------------------------------
    # SMS STOP webhook — mirror inbound opt-outs into our DNC table so the
    # dialer doesn't re-call someone who replied STOP.
    # ------------------------------------------------------------------
    @app.post("/sms-webhook")
    async def sms_webhook(req: Request) -> dict:
        try:
            body = await req.json()
        except Exception:
            form = await req.form()
            body = dict(form)
        text = (body.get("Body") or body.get("body") or body.get("message") or "").strip().lower()
        from_num = body.get("From") or body.get("from") or body.get("from_number") or ""
        if text in {"stop", "stop all", "unsubscribe", "cancel", "quit", "end"} and from_num:
            db.add_dnc(from_num, reason="inbound_stop")
            logger.info("DNC added from SMS STOP: %s", from_num)
        return {"ok": True}

    # ------------------------------------------------------------------
    # SSE — per-call live feed. Closes when the call ends.
    # ------------------------------------------------------------------
    @app.get("/api/calls/{call_id}/events")
    async def call_events(call_id: str) -> StreamingResponse:
        topic = f"call:{call_id}"

        async def stream():
            # 1. Snapshot so late-joining clients see current state.
            snap = db.get_call_snapshot(call_id)
            yield _sse("snapshot", snap)
            if not snap.get("found"):
                yield _sse("ended", {"reason": "not_found"})
                return
            if snap["call"].get("ended_at"):
                yield _sse("ended", {"reason": "already_ended"})
                return

            q = await events.subscribe(topic)
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        # Keep-alive comment — keeps proxies from closing the stream.
                        yield ": keepalive\n\n"
                        # After the timeout, check if the call ended while we waited.
                        if db.is_call_ended(call_id):
                            yield _sse("ended", {"reason": "post_timeout"})
                            return
                        continue

                    yield _sse(ev["type"], ev["data"])
                    if ev["type"] == "ended":
                        return
            finally:
                await events.unsubscribe(topic, q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # SSE — per-campaign progress. Closes when all voters are resolved.
    # ------------------------------------------------------------------
    @app.get("/api/campaigns/{campaign_id}/events")
    async def campaign_events(campaign_id: int) -> StreamingResponse:
        topic = f"campaign:{campaign_id}"

        async def stream():
            yield _sse("snapshot", {
                "campaign": db.get_campaign(campaign_id),
                "voters": db.list_campaign_voters(campaign_id),
                "results": db.campaign_results(campaign_id),
            })
            if db.active_voter_count(campaign_id) == 0:
                yield _sse("idle", {"campaign_id": campaign_id})
                return

            q = await events.subscribe(topic)
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=30.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        if db.active_voter_count(campaign_id) == 0:
                            yield _sse("idle", {"campaign_id": campaign_id})
                            return
                        continue

                    yield _sse(ev["type"], ev["data"])
                    if ev["type"] == "idle":
                        return
                    if db.active_voter_count(campaign_id) == 0:
                        yield _sse("idle", {"campaign_id": campaign_id})
                        return
            finally:
                await events.unsubscribe(topic, q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # Static dashboard
    # ------------------------------------------------------------------
    web_dir = Path(__file__).parent / "web"
    if web_dir.exists():
        server.serve_static_files(str(web_dir))

    return server


def _sse(event_type: str, data) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


server = create_server()
app = server.app


if __name__ == "__main__":
    server.run()
