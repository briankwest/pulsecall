"""SQLite persistence layer for PulseCall.

Schema:
    voter_lists           named pools of voters (e.g. "Boston Women 25-40")
    voters                belong to a list; phone is unique within a list
    campaigns             one polling script per campaign
    questions             per-campaign, ordered
    campaign_lists        m:n — which lists are used by which campaigns
    campaign_voter_state  per-(campaign, voter) dial status (pending/calling/...)
    calls                 one row per outbound/inbound attempt
    answers               one row per recorded response
    dnc_list              global do-not-call registry (across all campaigns/lists)
"""
import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

import config

DB_PATH = Path(__file__).parent / config.DATABASE_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voter_lists (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS voters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id     INTEGER NOT NULL REFERENCES voter_lists(id) ON DELETE CASCADE,
    phone       TEXT NOT NULL,
    first_name  TEXT,
    last_name   TEXT,
    zip_code    TEXT,
    gender      TEXT CHECK(gender IN ('M','F','NB','U') OR gender IS NULL),
    age_band    TEXT,  -- e.g. '18-24', '25-34', '35-44', '45-64', '65+'
    party       TEXT,  -- e.g. 'DEM','REP','IND','OTHER'
    UNIQUE(list_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_voters_list ON voters(list_id);
CREATE INDEX IF NOT EXISTS idx_voters_phone ON voters(phone);

CREATE TABLE IF NOT EXISTS campaigns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    script_intro   TEXT NOT NULL,
    status         TEXT CHECK(status IN ('draft','running','paused','completed'))
                   DEFAULT 'draft',
    caller_id      TEXT,
    max_concurrent INTEGER DEFAULT 2,
    created_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    prompt_text   TEXT NOT NULL,
    answer_type   TEXT CHECK(answer_type IN ('yesno','multi','scale','open')) NOT NULL,
    choices_json  TEXT,
    confirm       INTEGER DEFAULT 0,
    UNIQUE(campaign_id, ordinal)
);

CREATE TABLE IF NOT EXISTS campaign_lists (
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    list_id     INTEGER NOT NULL REFERENCES voter_lists(id) ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, list_id)
);

CREATE TABLE IF NOT EXISTS campaign_voter_state (
    campaign_id  INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    voter_id     INTEGER NOT NULL REFERENCES voters(id) ON DELETE CASCADE,
    status       TEXT CHECK(status IN ('pending','calling','completed','failed','dnc','optout'))
                 DEFAULT 'pending',
    attempts     INTEGER DEFAULT 0,
    last_call_id TEXT,
    updated_at   TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (campaign_id, voter_id)
);
CREATE INDEX IF NOT EXISTS idx_cvs_campaign_status
    ON campaign_voter_state(campaign_id, status);

CREATE TABLE IF NOT EXISTS calls (
    call_id      TEXT PRIMARY KEY,
    campaign_id  INTEGER NOT NULL,
    voter_id     INTEGER NOT NULL,
    started_at   TEXT DEFAULT (datetime('now')),
    ended_at     TEXT,
    outcome      TEXT,
    summary      TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_campaign ON calls(campaign_id);

CREATE TABLE IF NOT EXISTS answers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id      TEXT NOT NULL REFERENCES calls(call_id) ON DELETE CASCADE,
    question_id  INTEGER NOT NULL REFERENCES questions(id),
    value        TEXT NOT NULL,
    answered_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_answers_call ON answers(call_id);
CREATE INDEX IF NOT EXISTS idx_answers_question ON answers(question_id);

CREATE TABLE IF NOT EXISTS dnc_list (
    phone        TEXT PRIMARY KEY,
    reason       TEXT,
    source_call  TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)


def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(r) if r else None


# ======================================================================
# Voter lists
# ======================================================================

def create_list(name: str, description: Optional[str] = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO voter_lists (name, description) VALUES (?, ?)",
            (name, description),
        )
        return cur.lastrowid


def update_list(list_id: int, name: Optional[str] = None,
                description: Optional[str] = None) -> None:
    fields, args = [], []
    if name is not None:
        fields.append("name=?"); args.append(name)
    if description is not None:
        fields.append("description=?"); args.append(description)
    if not fields:
        return
    args.append(list_id)
    with connect() as conn:
        conn.execute(f"UPDATE voter_lists SET {', '.join(fields)} WHERE id=?", args)


def delete_list(list_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM voter_lists WHERE id=?", (list_id,))


def get_list(list_id: int) -> Optional[dict]:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM voter_lists WHERE id=?", (list_id,)
        ).fetchone())


def list_lists() -> list[dict]:
    """All lists with voter counts and gender breakdown."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.*, "
            "  (SELECT COUNT(*) FROM voters v WHERE v.list_id=l.id) AS voter_count, "
            "  (SELECT COUNT(*) FROM voters v WHERE v.list_id=l.id AND v.gender='M') AS male_count, "
            "  (SELECT COUNT(*) FROM voters v WHERE v.list_id=l.id AND v.gender='F') AS female_count, "
            "  (SELECT COUNT(*) FROM voters v WHERE v.list_id=l.id AND (v.gender IS NULL OR v.gender NOT IN ('M','F'))) AS other_count "
            "FROM voter_lists l ORDER BY l.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_campaigns_using_list(list_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.* FROM campaigns c "
            "JOIN campaign_lists cl ON cl.campaign_id=c.id "
            "WHERE cl.list_id=? ORDER BY c.created_at DESC",
            (list_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ======================================================================
# Voters
# ======================================================================

_VOTER_COLUMNS = ("first_name", "last_name", "zip_code", "gender", "age_band", "party")


def add_voter(list_id: int, phone: str, **fields) -> Optional[int]:
    """Insert a voter; also create campaign_voter_state rows for every campaign
    already linked to this list. Returns voter_id (new or existing)."""
    allowed = {k: v for k, v in fields.items() if k in _VOTER_COLUMNS}
    cols = ["list_id", "phone", *allowed.keys()]
    vals = [list_id, phone, *allowed.values()]

    with connect() as conn:
        try:
            cur = conn.execute(
                f"INSERT INTO voters ({','.join(cols)}) "
                f"VALUES ({','.join(['?'] * len(vals))})",
                vals,
            )
            voter_id = cur.lastrowid
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM voters WHERE list_id=? AND phone=?",
                (list_id, phone),
            ).fetchone()
            if not row:
                return None
            voter_id = row["id"]

        # Propagate to campaigns using this list.
        status = "dnc" if _is_dnc_locked(conn, phone) else "pending"
        camps = conn.execute(
            "SELECT campaign_id FROM campaign_lists WHERE list_id=?", (list_id,)
        ).fetchall()
        for c in camps:
            conn.execute(
                "INSERT OR IGNORE INTO campaign_voter_state (campaign_id, voter_id, status) "
                "VALUES (?, ?, ?)",
                (c["campaign_id"], voter_id, status),
            )
        return voter_id


def bulk_add_voters(list_id: int, voters: Iterable[dict]) -> dict:
    added = 0; skipped = 0
    for v in voters:
        phone = (v.get("phone") or "").strip()
        if not phone:
            skipped += 1; continue
        if add_voter(list_id, phone,
                     first_name=v.get("first_name"),
                     last_name=v.get("last_name"),
                     zip_code=v.get("zip_code"),
                     gender=_normalize_gender(v.get("gender")),
                     age_band=v.get("age_band"),
                     party=v.get("party")):
            added += 1
        else:
            skipped += 1
    return {"added": added, "skipped": skipped}


def parse_voter_csv(text: str) -> list[dict]:
    """Parse a CSV blob into voter dicts. Header row is optional; if absent
    the columns are assumed to be: phone, first_name, last_name, zip_code,
    gender, age_band, party."""
    text = (text or "").strip()
    if not text:
        return []
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    default_order = ["phone", "first_name", "last_name", "zip_code", "gender", "age_band", "party"]
    known = {"phone", "first_name", "last_name", "zip_code", "gender", "age_band", "party"}
    # Detect header: first row contains any known column name
    if any(h in known for h in header):
        cols = [h if h in known else None for h in header]
        data_rows = rows[1:]
    else:
        cols = default_order[:len(rows[0])]
        data_rows = rows
    out = []
    for r in data_rows:
        d = {}
        for i, val in enumerate(r):
            if i < len(cols) and cols[i]:
                d[cols[i]] = (val or "").strip() or None
        if d.get("phone"):
            out.append(d)
    return out


def get_voter(voter_id: int) -> Optional[dict]:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM voters WHERE id=?", (voter_id,)
        ).fetchone())


def update_voter(voter_id: int, **fields) -> None:
    allowed = {k: v for k, v in fields.items() if k in (*_VOTER_COLUMNS, "phone")}
    if "gender" in allowed:
        allowed["gender"] = _normalize_gender(allowed["gender"])
    if not allowed:
        return
    sets = ", ".join(f"{k}=?" for k in allowed)
    with connect() as conn:
        conn.execute(f"UPDATE voters SET {sets} WHERE id=?",
                     (*allowed.values(), voter_id))


def delete_voter(voter_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM voters WHERE id=?", (voter_id,))


def list_voters_in_list(list_id: int, gender: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM voters WHERE list_id=?"
    args = [list_id]
    if gender:
        q += " AND gender=?"; args.append(_normalize_gender(gender))
    q += " ORDER BY id"
    with connect() as conn:
        rows = conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]


def list_campaign_voters(campaign_id: int) -> list[dict]:
    """Voters across all lists linked to a campaign, joined with their state."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.*, cvs.status, cvs.attempts, cvs.last_call_id, l.name AS list_name "
            "FROM voters v "
            "JOIN campaign_voter_state cvs ON cvs.voter_id=v.id "
            "JOIN voter_lists l ON l.id=v.list_id "
            "WHERE cvs.campaign_id=? "
            "ORDER BY v.id",
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _normalize_gender(g: Optional[str]) -> Optional[str]:
    if not g:
        return None
    g = g.strip().upper()
    mapping = {
        "M": "M", "MALE": "M",
        "F": "F", "FEMALE": "F",
        "NB": "NB", "NONBINARY": "NB", "NON-BINARY": "NB", "X": "NB",
        "U": "U", "UNKNOWN": "U", "": None,
    }
    return mapping.get(g, "U")


# ======================================================================
# Campaigns
# ======================================================================

def create_campaign(name: str, script_intro: str,
                    caller_id: Optional[str] = None,
                    max_concurrent: int = 2,
                    list_ids: Optional[list[int]] = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, script_intro, caller_id, max_concurrent) "
            "VALUES (?, ?, ?, ?)",
            (name, script_intro, caller_id, max_concurrent),
        )
        campaign_id = cur.lastrowid
        for lid in list_ids or []:
            _link_list(conn, campaign_id, lid)
        return campaign_id


def update_campaign(campaign_id: int, **fields) -> None:
    """Update campaign metadata. Always-editable fields: name, script_intro,
    caller_id, max_concurrent. Questions/lists are managed via their own endpoints."""
    allowed = {}
    for k in ("name", "script_intro", "caller_id", "max_concurrent"):
        if k in fields and fields[k] is not None:
            allowed[k] = fields[k]
    if not allowed:
        return
    sets = ", ".join(f"{k}=?" for k in allowed)
    with connect() as conn:
        conn.execute(
            f"UPDATE campaigns SET {sets} WHERE id=?",
            (*allowed.values(), campaign_id),
        )


def delete_campaign(campaign_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))


def get_campaign(campaign_id: int) -> Optional[dict]:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone())


def list_campaigns() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT c.*, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id) AS total_voters, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status='completed') AS completed, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status='pending') AS pending, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status='calling') AS calling, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status IN ('dnc','optout')) AS dnc, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status='failed') AS failed "
            "FROM campaigns c ORDER BY c.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def set_campaign_status(campaign_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, campaign_id))


# ---- Campaign ↔ list linking ----

def _link_list(conn: sqlite3.Connection, campaign_id: int, list_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO campaign_lists (campaign_id, list_id) VALUES (?, ?)",
        (campaign_id, list_id),
    )
    # Seed campaign_voter_state for every voter already in this list.
    voters = conn.execute(
        "SELECT id, phone FROM voters WHERE list_id=?", (list_id,)
    ).fetchall()
    for v in voters:
        status = "dnc" if _is_dnc_locked(conn, v["phone"]) else "pending"
        conn.execute(
            "INSERT OR IGNORE INTO campaign_voter_state (campaign_id, voter_id, status) "
            "VALUES (?, ?, ?)",
            (campaign_id, v["id"], status),
        )


def link_list(campaign_id: int, list_id: int) -> None:
    with connect() as conn:
        _link_list(conn, campaign_id, list_id)


def unlink_list(campaign_id: int, list_id: int) -> None:
    with connect() as conn:
        # Remove only the state rows for voters that came ONLY from this list;
        # if another linked list also contains a voter with the same phone we
        # keep their state row. Simpler rule: only remove states whose voter
        # is in this list (voters have list_id in their row, so if we unlink
        # list L, the voter rows in L still exist but no campaign_list entry
        # joins them — the dialer query won't find them anyway). For
        # cleanliness, drop state rows whose voter.list_id == list_id.
        conn.execute(
            "DELETE FROM campaign_voter_state "
            "WHERE campaign_id=? AND voter_id IN "
            "      (SELECT id FROM voters WHERE list_id=?)",
            (campaign_id, list_id),
        )
        conn.execute(
            "DELETE FROM campaign_lists WHERE campaign_id=? AND list_id=?",
            (campaign_id, list_id),
        )


def get_campaign_lists(campaign_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.* FROM voter_lists l "
            "JOIN campaign_lists cl ON cl.list_id=l.id "
            "WHERE cl.campaign_id=? ORDER BY l.name",
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# ======================================================================
# Questions
# ======================================================================

def add_question(campaign_id: int, ordinal: int, prompt_text: str, answer_type: str,
                 choices: Optional[list[str]] = None, confirm: bool = False) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO questions (campaign_id, ordinal, prompt_text, answer_type, choices_json, confirm) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (campaign_id, ordinal, prompt_text, answer_type,
             json.dumps(choices) if choices else None, 1 if confirm else 0),
        )
        return cur.lastrowid


def replace_questions(campaign_id: int, questions: list[dict]) -> None:
    """Used on campaign edit while status='draft' — wipe and re-insert."""
    with connect() as conn:
        conn.execute("DELETE FROM questions WHERE campaign_id=?", (campaign_id,))
        for i, q in enumerate(questions, start=1):
            conn.execute(
                "INSERT INTO questions (campaign_id, ordinal, prompt_text, answer_type, choices_json, confirm) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (campaign_id, i, q["prompt_text"], q["answer_type"],
                 json.dumps(q.get("choices")) if q.get("choices") else None,
                 1 if q.get("confirm") else 0),
            )


def get_questions(campaign_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM questions WHERE campaign_id=? ORDER BY ordinal",
            (campaign_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["choices"] = json.loads(d["choices_json"]) if d["choices_json"] else None
            d["confirm"] = bool(d["confirm"])
            out.append(d)
        return out


# ======================================================================
# Campaign-voter state & dialer queries
# ======================================================================

def pending_voters(campaign_id: int, limit: int = 500) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.*, cvs.status, cvs.attempts "
            "FROM campaign_voter_state cvs "
            "JOIN voters v ON v.id=cvs.voter_id "
            "WHERE cvs.campaign_id=? AND cvs.status='pending' "
            "LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def set_voter_state(campaign_id: int, voter_id: int, status: str,
                    call_id: Optional[str] = None) -> None:
    with connect() as conn:
        if call_id is not None:
            conn.execute(
                "UPDATE campaign_voter_state SET status=?, last_call_id=?, "
                "attempts=attempts+1, updated_at=datetime('now') "
                "WHERE campaign_id=? AND voter_id=?",
                (status, call_id, campaign_id, voter_id),
            )
        else:
            conn.execute(
                "UPDATE campaign_voter_state SET status=?, "
                "updated_at=datetime('now') "
                "WHERE campaign_id=? AND voter_id=?",
                (status, campaign_id, voter_id),
            )


def get_voter_state(campaign_id: int, voter_id: int) -> Optional[dict]:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM campaign_voter_state WHERE campaign_id=? AND voter_id=?",
            (campaign_id, voter_id),
        ).fetchone())


def calling_voter_count(campaign_id: int) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM campaign_voter_state "
            "WHERE campaign_id=? AND status='calling'",
            (campaign_id,),
        ).fetchone()
        return int(row["n"])


def reclaim_stuck_calling(campaign_id: int) -> int:
    """Flip any rows stuck in 'calling' back to 'pending'. Call on start and
    on startup-recovery — there shouldn't be a live dial after either event."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE campaign_voter_state SET status='pending', "
            "updated_at=datetime('now') "
            "WHERE campaign_id=? AND status='calling'",
            (campaign_id,),
        )
        return cur.rowcount


def reset_failed_voters(campaign_id: int) -> int:
    """Flip 'failed' rows (and any stuck 'calling') back to 'pending'.

    Leaves completed/dnc alone — this is the safe 'retry what went wrong'
    action, not a destructive reset.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE campaign_voter_state SET status='pending', "
            "updated_at=datetime('now') "
            "WHERE campaign_id=? AND status IN ('failed','calling')",
            (campaign_id,),
        )
        return cur.rowcount


def reset_all_voters(campaign_id: int) -> dict:
    """DESTRUCTIVE: drop every call + answer for this campaign, reset every
    voter state (except those on the global DNC list) to 'pending'. Intended
    for testing — the UI confirms before invoking this."""
    with connect() as conn:
        answers = conn.execute(
            "DELETE FROM answers WHERE call_id IN "
            "(SELECT call_id FROM calls WHERE campaign_id=?)",
            (campaign_id,),
        ).rowcount
        calls = conn.execute(
            "DELETE FROM calls WHERE campaign_id=?", (campaign_id,)
        ).rowcount
        # Reset state for any voter whose phone is NOT on the global DNC list.
        reset = conn.execute(
            "UPDATE campaign_voter_state SET status='pending', attempts=0, "
            "last_call_id=NULL, updated_at=datetime('now') "
            "WHERE campaign_id=? AND voter_id IN ("
            "  SELECT v.id FROM voters v "
            "  WHERE v.id = campaign_voter_state.voter_id "
            "    AND v.phone NOT IN (SELECT phone FROM dnc_list)"
            ")",
            (campaign_id,),
        ).rowcount
        # DNC-listed voters stay on 'dnc'.
        dnc = conn.execute(
            "UPDATE campaign_voter_state SET status='dnc' "
            "WHERE campaign_id=? AND voter_id IN ("
            "  SELECT id FROM voters WHERE phone IN (SELECT phone FROM dnc_list)"
            ")",
            (campaign_id,),
        ).rowcount
        return {
            "voters_reset": reset,
            "voters_dnc": dnc,
            "calls_deleted": calls,
            "answers_deleted": answers,
        }


def bulk_add_dnc(entries: list[dict]) -> dict:
    """Accept a list of {phone, reason?} dicts; add each via add_dnc (which
    also cascades to campaign_voter_state). Returns counts."""
    added = 0; skipped = 0
    for e in entries:
        phone = (e.get("phone") or "").strip()
        if not phone:
            skipped += 1; continue
        add_dnc(phone, reason=(e.get("reason") or "manual"))
        added += 1
    return {"added": added, "skipped": skipped}


def active_voter_count(campaign_id: int) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM campaign_voter_state "
            "WHERE campaign_id=? AND status IN ('pending','calling')",
            (campaign_id,),
        ).fetchone()
        return int(row["n"])


def find_voter_by_phone(phone: str) -> Optional[dict]:
    """For inbound: find the best match for a caller across all campaigns.
    Prefers 'pending' > 'failed' > 'calling' > 'completed' — most recent campaign wins."""
    with connect() as conn:
        row = conn.execute(
            "SELECT v.*, cvs.status AS state_status, cvs.campaign_id, "
            "       c.name AS campaign_name, c.script_intro, c.status AS campaign_status "
            "FROM voters v "
            "LEFT JOIN campaign_voter_state cvs ON cvs.voter_id=v.id "
            "LEFT JOIN campaigns c ON c.id=cvs.campaign_id "
            "WHERE v.phone=? "
            "ORDER BY CASE cvs.status "
            "  WHEN 'pending'   THEN 0 "
            "  WHEN 'failed'    THEN 1 "
            "  WHEN 'calling'   THEN 2 "
            "  WHEN 'completed' THEN 3 "
            "  ELSE 4 END, "
            "  c.created_at DESC "
            "LIMIT 1",
            (phone,),
        ).fetchone()
        return dict(row) if row else None


# ======================================================================
# Calls & answers
# ======================================================================

def create_call(call_id: str, campaign_id: int, voter_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO calls (call_id, campaign_id, voter_id) VALUES (?, ?, ?)",
            (call_id, campaign_id, voter_id),
        )


def end_call(call_id: str, outcome: str, summary: Optional[str] = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE calls SET ended_at=datetime('now'), outcome=?, summary=? "
            "WHERE call_id=?",
            (outcome, summary, call_id),
        )


def get_call(call_id: str) -> Optional[dict]:
    with connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM calls WHERE call_id=?", (call_id,)
        ).fetchone())


def is_call_ended(call_id: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT ended_at FROM calls WHERE call_id=?", (call_id,)
        ).fetchone()
        return bool(row) and row["ended_at"] is not None


def get_call_snapshot(call_id: str) -> dict:
    with connect() as conn:
        call = conn.execute(
            "SELECT * FROM calls WHERE call_id=?", (call_id,)
        ).fetchone()
        if not call:
            return {"call_id": call_id, "found": False}
        voter = conn.execute(
            "SELECT * FROM voters WHERE id=?", (call["voter_id"],)
        ).fetchone()
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id=?", (call["campaign_id"],)
        ).fetchone()
        questions = conn.execute(
            "SELECT * FROM questions WHERE campaign_id=? ORDER BY ordinal",
            (call["campaign_id"],),
        ).fetchall()
        answers = conn.execute(
            "SELECT * FROM answers WHERE call_id=? ORDER BY answered_at", (call_id,)
        ).fetchall()
        return {
            "found": True,
            "call": dict(call),
            "voter": dict(voter) if voter else None,
            "campaign": dict(campaign) if campaign else None,
            "questions": [dict(q) for q in questions],
            "answers": [dict(a) for a in answers],
        }


def insert_answer(call_id: str, question_id: int, value: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO answers (call_id, question_id, value) VALUES (?, ?, ?)",
            (call_id, question_id, value),
        )
        return cur.lastrowid


def answers_for_call(call_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.*, q.prompt_text, q.ordinal FROM answers a "
            "JOIN questions q ON q.id=a.question_id WHERE a.call_id=? "
            "ORDER BY q.ordinal",
            (call_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def campaign_results(campaign_id: int) -> dict:
    """Per-question distribution with counts AND percentages."""
    with connect() as conn:
        questions = conn.execute(
            "SELECT * FROM questions WHERE campaign_id=? ORDER BY ordinal",
            (campaign_id,),
        ).fetchall()
        out = []
        for q in questions:
            rows = conn.execute(
                "SELECT a.value, COUNT(*) AS n FROM answers a "
                "JOIN calls c ON c.call_id=a.call_id "
                "WHERE c.campaign_id=? AND a.question_id=? "
                "GROUP BY a.value ORDER BY n DESC",
                (campaign_id, q["id"]),
            ).fetchall()
            total = sum(r["n"] for r in rows)
            dist = []
            for r in rows:
                pct = round(100.0 * r["n"] / total, 1) if total else 0.0
                dist.append({"value": r["value"], "count": r["n"], "percent": pct})
            out.append({
                "question_id": q["id"],
                "ordinal": q["ordinal"],
                "prompt_text": q["prompt_text"],
                "answer_type": q["answer_type"],
                "total_responses": total,
                "distribution": dist,
            })
        return {"campaign_id": campaign_id, "questions": out}


def reports_overview() -> dict:
    """Cross-campaign rollup for the /reports page."""
    with connect() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS n_campaigns, "
            "  (SELECT COUNT(*) FROM voter_lists) AS n_lists, "
            "  (SELECT COUNT(*) FROM voters) AS n_voters, "
            "  (SELECT COUNT(*) FROM calls) AS n_calls, "
            "  (SELECT COUNT(*) FROM calls WHERE outcome='completed') AS n_completed, "
            "  (SELECT COUNT(*) FROM calls WHERE outcome='dnc') AS n_dnc, "
            "  (SELECT COUNT(*) FROM calls WHERE outcome='failed') AS n_failed, "
            "  (SELECT COUNT(*) FROM calls WHERE outcome='no_answer') AS n_no_answer, "
            "  (SELECT COUNT(*) FROM answers) AS n_answers, "
            "  (SELECT COUNT(*) FROM dnc_list) AS n_dnc_global "
            "FROM campaigns"
        ).fetchone()
        campaigns = conn.execute(
            "SELECT c.*, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id) AS total_voters, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status='completed') AS completed, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status IN ('dnc','optout')) AS dnc, "
            "  (SELECT COUNT(*) FROM campaign_voter_state s WHERE s.campaign_id=c.id AND s.status='failed') AS failed, "
            "  (SELECT COUNT(*) FROM answers a JOIN calls cc ON cc.call_id=a.call_id WHERE cc.campaign_id=c.id) AS answer_count "
            "FROM campaigns c ORDER BY c.created_at DESC"
        ).fetchall()
        return {
            "totals": dict(totals),
            "campaigns": [dict(r) for r in campaigns],
        }


def export_answers_csv(campaign_id: int) -> str:
    """Flat CSV of every answer joined with voter + question + call metadata."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT "
            "  c.started_at  AS call_started, "
            "  c.call_id     AS call_id, "
            "  c.outcome     AS call_outcome, "
            "  v.phone       AS phone, "
            "  v.first_name  AS first_name, "
            "  v.last_name   AS last_name, "
            "  v.gender      AS gender, "
            "  v.age_band    AS age_band, "
            "  v.party       AS party, "
            "  v.zip_code    AS zip_code, "
            "  l.name        AS list_name, "
            "  q.ordinal     AS q_ordinal, "
            "  q.prompt_text AS question, "
            "  q.answer_type AS answer_type, "
            "  a.value       AS answer, "
            "  a.answered_at AS answered_at "
            "FROM answers a "
            "JOIN calls c      ON c.call_id=a.call_id "
            "JOIN questions q  ON q.id=a.question_id "
            "JOIN voters v     ON v.id=c.voter_id "
            "JOIN voter_lists l ON l.id=v.list_id "
            "WHERE c.campaign_id=? "
            "ORDER BY c.started_at, q.ordinal",
            (campaign_id,),
        ).fetchall()
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))
    return buf.getvalue()


def export_voters_csv(campaign_id: int) -> str:
    """Voter roster for a campaign with dial status."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.phone, v.first_name, v.last_name, v.gender, v.age_band, "
            "       v.party, v.zip_code, l.name AS list_name, "
            "       cvs.status, cvs.attempts, cvs.last_call_id, cvs.updated_at "
            "FROM campaign_voter_state cvs "
            "JOIN voters v      ON v.id=cvs.voter_id "
            "JOIN voter_lists l ON l.id=v.list_id "
            "WHERE cvs.campaign_id=? ORDER BY v.id",
            (campaign_id,),
        ).fetchall()
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=rows[0].keys())
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))
    return buf.getvalue()


# ======================================================================
# DNC
# ======================================================================

def _is_dnc_locked(conn: sqlite3.Connection, phone: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM dnc_list WHERE phone=?", (phone,)
    ).fetchone() is not None


def is_dnc(phone: str) -> bool:
    with connect() as conn:
        return _is_dnc_locked(conn, phone)


def add_dnc(phone: str, reason: str = "user_request",
            source_call: Optional[str] = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO dnc_list (phone, reason, source_call) "
            "VALUES (?, ?, ?)", (phone, reason, source_call),
        )
        # Cascade to every campaign_voter_state row that touches this number.
        conn.execute(
            "UPDATE campaign_voter_state SET status='dnc', updated_at=datetime('now') "
            "WHERE status IN ('pending','calling') AND voter_id IN "
            "      (SELECT id FROM voters WHERE phone=?)",
            (phone,),
        )


def remove_dnc(phone: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM dnc_list WHERE phone=?", (phone,))


def list_dnc() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM dnc_list ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
