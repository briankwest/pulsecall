"""Outbound dialer — walks voter queues, respects DNC, places calls via SignalWire REST."""
import logging
import threading
import time
from typing import Optional
from urllib.parse import urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from signalwire.rest.client import RestClient
from signalwire.rest._base import SignalWireRestError

import config
import db
import events

logger = logging.getLogger("pulsecall.dialer")


def _callback_url(campaign_id: int, voter_id: int) -> str:
    """Build the /outbound SWML URL SignalWire will fetch when the call connects.

    Basic auth is embedded in the URL so SignalWire's unauthenticated webhook
    fetch still passes our /outbound route's auth gate.
    """
    base = (config.SWML_PROXY_URL_BASE or f"http://localhost:{config.PORT}").rstrip("/")
    user = config.SWML_BASIC_AUTH_USER
    pwd = config.SWML_BASIC_AUTH_PASSWORD

    if user and pwd and "://" in base:
        scheme, rest = base.split("://", 1)
        base = f"{scheme}://{user}:{pwd}@{rest}"

    qs = urlencode({"campaign_id": campaign_id, "voter_id": voter_id})
    return f"{base}/outbound?{qs}"


class OutboundDialer:
    """One BackgroundScheduler for the process; per-campaign job IDs.

    Source-of-truth separation:
      - `campaigns.status` (DB) is the durable campaign status: draft/running/paused/completed.
      - `_running_campaigns` + scheduler jobs (in-memory) are the runtime view.
      A DB status change never happens inside a scheduler-only helper, and
      vice versa, so the two can never disagree for long.
    """

    def __init__(self) -> None:
        self._scheduler = BackgroundScheduler()
        self._scheduler.start()
        self._running_campaigns: set[int] = set()
        self._lock = threading.Lock()
        self._client: Optional[RestClient] = None

    # ------------------------------------------------------------------
    def _rest_client(self) -> RestClient:
        if self._client is None:
            self._client = RestClient(
                project=config.SIGNALWIRE_PROJECT_ID,
                token=config.SIGNALWIRE_API_TOKEN,
                host=config.SIGNALWIRE_SPACE,
            )
        return self._client

    # ---- runtime-only helpers (never touch DB status) ----------------

    def _register_job(self, campaign_id: int) -> None:
        """Install the interval job that drains this campaign's queue."""
        job_id = f"campaign-{campaign_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        self._scheduler.add_job(
            self._drain_queue, args=(campaign_id,), id=job_id,
            trigger="interval", seconds=30, next_run_time=None,
        )
        with self._lock:
            self._running_campaigns.add(campaign_id)

    def _unregister_job(self, campaign_id: int) -> None:
        """Tear down the interval job for this campaign."""
        with self._lock:
            self._running_campaigns.discard(campaign_id)
        job_id = f"campaign-{campaign_id}"
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)

    def is_running(self, campaign_id: int) -> bool:
        """Is the scheduler actively draining this campaign right now?"""
        return (campaign_id in self._running_campaigns
                and self._scheduler.get_job(f"campaign-{campaign_id}") is not None)

    # ---- user-facing actions -----------------------------------------

    def start_campaign(self, campaign_id: int) -> dict:
        campaign = db.get_campaign(campaign_id)
        if not campaign:
            return {"ok": False, "error": f"campaign {campaign_id} not found"}

        if campaign["status"] == "completed":
            # Only reject if there's genuinely nothing to do. If a caller
            # just ran retry-failed (or added voters), pending_voters will be
            # non-empty and we should proceed — otherwise this forces the user
            # to call reset for a perfectly valid retry workflow.
            if not db.pending_voters(campaign_id, limit=1):
                return {"ok": False, "error":
                        "campaign is 'completed' — no voters left to dial. "
                        "Use retry-failed to redial failed numbers, or reset to redial everyone."}

        # Reclaim any rows stuck in 'calling' (a previous drain crashed mid-dial
        # or the server was restarted while a call was open).
        reclaimed = db.reclaim_stuck_calling(campaign_id)
        if reclaimed:
            logger.info("reclaimed %d stuck 'calling' rows for campaign %s",
                        reclaimed, campaign_id)

        db.set_campaign_status(campaign_id, "running")

        if self.is_running(campaign_id):
            return {"ok": True, "status": "already_running"}

        self._register_job(campaign_id)
        # Kick an immediate drain so the UI sees action right away. Drain in a
        # loop until either the queue is empty or a real call is in flight —
        # otherwise we'd stop after max_concurrent failures and wait 30s for
        # the next scheduler tick.
        threading.Thread(
            target=self._kick_drain, args=(campaign_id,), daemon=True
        ).start()
        return {"ok": True, "status": "started"}

    def _kick_drain(self, campaign_id: int) -> None:
        """Drain in a loop while progress is being made AND no real calls are
        in flight. Hands back to the scheduler (30s interval) once a dial
        actually connects or the queue is exhausted."""
        for _ in range(100):  # hard cap, defensive against loops
            self._drain_queue(campaign_id)
            campaign = db.get_campaign(campaign_id)
            if not campaign or campaign["status"] != "running":
                return  # completion fired inside _drain_queue, or someone paused
            if db.calling_voter_count(campaign_id) > 0:
                return  # real call in flight — let the scheduler poll
            if not db.pending_voters(campaign_id, limit=1):
                return  # queue empty (shouldn't happen; _drain_queue marks completed)

    def pause_campaign(self, campaign_id: int) -> dict:
        """Stop the scheduler job. Only flip DB status → 'paused' when the
        campaign was actually 'running' — calling Pause on a 'completed'
        campaign should leave it 'completed'."""
        campaign = db.get_campaign(campaign_id)
        if campaign and campaign["status"] == "running":
            db.set_campaign_status(campaign_id, "paused")
        self._unregister_job(campaign_id)
        after = db.get_campaign(campaign_id) or {}
        return {"ok": True, "status": after.get("status", "paused")}

    def notify_call_ended(self, campaign_id: int) -> None:
        """Called from on_summary after a call finalizes. Triggers an immediate
        drain so the campaign can transition to 'completed' the moment its last
        voter resolves, instead of waiting up to 30s for the next scheduler tick.
        Also re-fills the in-flight pool when concurrency frees up."""
        threading.Thread(
            target=self._kick_drain, args=(campaign_id,), daemon=True
        ).start()

    def retry_failed(self, campaign_id: int) -> dict:
        """Flip 'failed' + stuck 'calling' rows back to 'pending' and start."""
        n = db.reset_failed_voters(campaign_id)
        result = self.start_campaign(campaign_id)
        result["reset"] = n
        return result

    def reset_campaign(self, campaign_id: int) -> dict:
        """Destructive: reset ALL non-DNC voters to 'pending', drop calls+answers.
        Intended for testing — a confirm dialog in the UI gates it."""
        self._unregister_job(campaign_id)
        counts = db.reset_all_voters(campaign_id)
        db.set_campaign_status(campaign_id, "draft")
        return {"ok": True, **counts}

    # ---- post-restart recovery ---------------------------------------

    def recover_running_campaigns(self) -> None:
        """Re-register scheduler jobs for campaigns that were 'running' in the
        DB when the process started (e.g., after a crash or restart). Also
        kick an immediate drain so the campaign picks up where it left off
        instead of waiting 30s for the first scheduler tick.

        Bonus: rescues campaigns that ended up 'running' with no active voters
        (e.g. pre-fix state where the completion-kick on the last call didn't
        fire) — marks them 'completed' immediately so the UI is accurate.
        """
        for c in db.list_campaigns():
            if c["status"] != "running":
                continue

            if db.active_voter_count(c["id"]) == 0:
                db.set_campaign_status(c["id"], "completed")
                logger.info("startup: campaign %s had no active voters — "
                            "marked completed", c["id"])
                events.publish(f"campaign:{c['id']}", "idle",
                               {"campaign_id": c["id"]})
                continue

            reclaimed = db.reclaim_stuck_calling(c["id"])
            if reclaimed:
                logger.info("startup: reclaimed %d stuck 'calling' rows for campaign %s",
                            reclaimed, c["id"])
            if not self.is_running(c["id"]):
                self._register_job(c["id"])
                logger.info("startup: re-registered drain job for campaign %s", c["id"])
            threading.Thread(
                target=self._kick_drain, args=(c["id"],), daemon=True
            ).start()

    # ---- the drain itself --------------------------------------------

    def _drain_queue(self, campaign_id: int) -> None:
        """Dial pending voters up to the campaign's concurrency cap."""
        campaign = db.get_campaign(campaign_id)
        if not campaign or campaign["status"] != "running":
            return

        max_concurrent = int(campaign.get("max_concurrent") or config.MAX_OUTBOUND_CONCURRENT)
        caller_id = campaign.get("caller_id") or config.SIGNALWIRE_PHONE_NUMBER
        if not caller_id:
            logger.error("Campaign %s has no caller_id and SIGNALWIRE_PHONE_NUMBER is unset",
                         campaign_id)
            return

        voters = db.pending_voters(campaign_id, limit=max_concurrent)
        if not voters:
            if db.active_voter_count(campaign_id) == 0:
                # Queue drained AND nothing in flight → campaign is complete.
                # Write 'completed' and stop the scheduler; do NOT go through
                # pause_campaign, which would incorrectly flip status to 'paused'.
                db.set_campaign_status(campaign_id, "completed")
                events.publish(f"campaign:{campaign_id}", "idle", {"campaign_id": campaign_id})
                self._unregister_job(campaign_id)
            return

        for voter in voters:
            phone = voter["phone"]
            if db.is_dnc(phone):
                db.set_voter_state(campaign_id, voter["id"], "dnc")
                events.publish(f"campaign:{campaign_id}", "voter_dnc",
                               {"voter_id": voter["id"], "phone": phone})
                continue

            # Mark as calling BEFORE placing so concurrent drains don't double-dial.
            db.set_voter_state(campaign_id, voter["id"], "calling")

            url = _callback_url(campaign_id, voter["id"])
            try:
                resp = self._rest_client().calling.dial(
                    to=phone, **{"from": caller_id}, url=url,
                )
                call_id = (resp or {}).get("id") or (resp or {}).get("call_id") or "unknown"
                logger.info("dialed campaign=%s voter=%s phone=%s call_id=%s",
                            campaign_id, voter["id"], phone, call_id)
                events.publish(f"campaign:{campaign_id}", "dial_placed", {
                    "voter_id": voter["id"], "phone": phone, "call_id": call_id,
                })
            except SignalWireRestError as e:
                logger.error("dial failed for voter=%s phone=%s: %s", voter["id"], phone, e)
                db.set_voter_state(campaign_id, voter["id"], "failed")
                events.publish(f"campaign:{campaign_id}", "dial_failed",
                               {"voter_id": voter["id"], "error": str(e)})
            except Exception as e:  # noqa: BLE001
                logger.exception("unexpected dial error for voter=%s", voter["id"])
                db.set_voter_state(campaign_id, voter["id"], "failed")
                events.publish(f"campaign:{campaign_id}", "dial_failed",
                               {"voter_id": voter["id"], "error": str(e)})

            time.sleep(0.5)

        # Post-drain: if every voter is now in a terminal state (all dials
        # failed, no in-flight 'calling' rows, no more 'pending'), transition
        # to 'completed' immediately rather than waiting 30s for the next tick.
        if db.active_voter_count(campaign_id) == 0:
            db.set_campaign_status(campaign_id, "completed")
            events.publish(f"campaign:{campaign_id}", "idle", {"campaign_id": campaign_id})
            self._unregister_job(campaign_id)

    # ------------------------------------------------------------------
    def configure_cron(self) -> None:
        spec = config.OUTBOUND_SCHEDULE
        if not spec:
            return
        try:
            trigger = CronTrigger.from_crontab(spec)
        except Exception as e:  # noqa: BLE001
            logger.warning("invalid OUTBOUND_SCHEDULE %r: %s", spec, e)
            return

        def _cron_kick() -> None:
            for c in db.list_campaigns():
                if c["status"] == "running":
                    self._drain_queue(c["id"])

        self._scheduler.add_job(_cron_kick, trigger=trigger, id="cron-kick",
                                replace_existing=True)

    def shutdown(self) -> None:
        try:
            self._scheduler.shutdown(wait=False)
        except Exception:
            pass


# Module-level singleton — server imports and uses this.
dialer = OutboundDialer()
