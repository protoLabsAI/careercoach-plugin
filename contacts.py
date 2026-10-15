"""Contacts — the people half of a job hunt: who you know where, and who you owe a reply.

Referrals convert far better than cold applications, so before applying the coach should ask
"who do I know there?". The answer comes from LinkedIn's own data export (Settings → Data privacy
→ Get a copy of your data → Connections), which the operator downloads themselves — nothing here
scrapes LinkedIn or looks up anyone's email.

``contacts.json`` holds one row per person: name, company, title, LinkedIn URL, where they came
from (``linkedin`` import / ``manual``), plus a touch log and an optional next action + due date
that the daily digest surfaces. Host-free; pure parsers, locked atomic store.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime

from . import profile as _store
from .companies import company_hint_key, company_key

StoreUnreadable = _store.StoreUnreadable

_LINKEDIN_HEADER = ("first name", "last name")


def parse_linkedin_connections(text: str) -> list[dict]:
    """Parse LinkedIn's ``Connections.csv``. The export opens with a few lines of notes before the
    real header (``First Name,Last Name,URL,Email Address,Company,Position,Connected On``), so the
    header row is found rather than assumed. Email is usually blank — LinkedIn only includes it
    when the connection allowed it."""
    lines = (text or "").lstrip("﻿").splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.lower().replace('"', "").startswith(",".join(_LINKEDIN_HEADER))),
        None,
    )
    if start is None:
        raise ValueError("this doesn't look like LinkedIn's Connections.csv (no 'First Name,Last Name,…' header row)")
    out = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = f"{r.get('first name', '')} {r.get('last name', '')}".strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "company": r.get("company", ""),
                "title": r.get("position", ""),
                "url": r.get("url", ""),
                "email": r.get("email address", ""),
                "connected_on": r.get("connected on", ""),
                "source": "linkedin",
            }
        )
    return out


def _path():
    return _store._dir() / "contacts.json"


def load_contacts() -> list[dict]:
    raw, err = _store._read_json(_path())
    if err:
        raise StoreUnreadable(_path(), err)
    if raw is not None and not isinstance(raw, list):
        raise StoreUnreadable(_path(), "not a JSON list")
    return list(raw or [])


def _save(rows: list[dict]) -> None:
    _store._write_store(_path(), json.dumps(rows, indent=2, ensure_ascii=False))


def _ident(c: dict) -> str:
    return (c.get("url") or "").rstrip(
        "/"
    ).lower() or f"{c.get('name', '').lower()}|{company_key(c.get('company', ''))}"


def merge_contacts(incoming: list[dict]) -> tuple[int, int]:
    """Upsert contacts by LinkedIn URL (else name + company). Returns ``(added, updated)``. An
    update refreshes company/title (people change jobs) and keeps the touch log + next action."""
    added = updated = 0
    with _store._locked("contacts"):
        rows = load_contacts()
        index = {_ident(r): r for r in rows}
        for c in incoming:
            cur = index.get(_ident(c))
            if cur is None:
                row = {**c, "touches": [], "next_action": "", "due": ""}
                rows.append(row)
                index[_ident(row)] = row
                added += 1
            else:
                for k in ("company", "title", "url", "email", "connected_on"):
                    if c.get(k):
                        cur[k] = c[k]
                updated += 1
        _save(rows)
    return added, updated


def at_company(company: str, rows: list[dict] | None = None) -> list[dict]:
    """Contacts whose current company is ``company`` — loosely matched ("Scale AI" finds "Scale"):
    a referral lead is a hint to check, not a record to merge."""
    key = company_hint_key(company)
    if not key:
        return []
    return [c for c in (load_contacts() if rows is None else rows) if company_hint_key(c.get("company", "")) == key]


def log_touch(name: str, company: str = "", *, note: str = "", next_action: str = "", due: str = "",
              title: str = "", url: str = "") -> dict:  # fmt: skip
    """Record a touch (message sent, call, intro asked) on a contact, creating them if new, and
    set the next action + its due date (YYYY-MM-DD). Returns the contact row."""
    if due:
        date.fromisoformat(due)  # ValueError on a malformed date, before anything is written
    today = datetime.now(UTC).date().isoformat()
    with _store._locked("contacts"):
        rows = load_contacts()
        nkey = name.strip().lower()
        ckey = company_key(company)
        cur = next(
            (
                r
                for r in rows
                if r.get("name", "").lower() == nkey and (not ckey or company_key(r.get("company", "")) == ckey)
            ),
            None,
        )
        if cur is None:
            cur = {"name": name.strip(), "company": company.strip(), "title": title, "url": url, "email": "",
                   "connected_on": "", "source": "manual", "touches": [], "next_action": "", "due": ""}  # fmt: skip
            rows.append(cur)
        if note:
            cur.setdefault("touches", []).append({"date": today, "note": note})
        if next_action or due:
            cur["next_action"] = next_action or cur.get("next_action", "")
            cur["due"] = due
        _save(rows)
        return cur


def due_followups(rows: list[dict] | None = None, today: date | None = None) -> list[dict]:
    """Contacts whose next action is due today or overdue, oldest first."""
    today = today or datetime.now(UTC).date()
    out = []
    for c in load_contacts() if rows is None else rows:
        if c.get("due"):
            try:
                if date.fromisoformat(c["due"]) <= today:
                    out.append(c)
            except ValueError:
                continue
    return sorted(out, key=lambda c: c["due"])
