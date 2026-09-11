"""Operator profile — structured, instance-scoped, always-injected, host-free.

The coach's answer to "who am I working for?". Before this existed the answer lived in a
markdown file the operator had to find in Finder, which meant two failures: the agent
re-interviewed for facts it already held (it had no cheap way to know what it knew), and the
person being described couldn't see the record. So the profile is now *state*, not a document.

**The profile is the single source of truth.** Everything that reads the operator's history —
the always-on block, ``careercoach_read_profile("experience")``, the drafting and scoring crew —
reads *this*. A workspace ``Resume/Experience.md`` is never a competing read source; it takes part
in two explicit, one-way moves:

  ``Experience.md``  --import (``parse_experience`` → ``plan_import`` → ``apply_import``)-->  ``profile.json``
  ``profile.json``   --export (``to_markdown``)-->  ``Resume/Experience (profile export).md``

and every write — the agent's (``update_field``) and an import's — is planned by the same
``_plan``, so an import obeys the same append/replace, size-cap and ``do_not_claim`` rules.

Two design rules earn their keep:

* **Completeness is first-class.** ``completeness()`` reports what's known *and what isn't*, and
  that goes in front of the model every turn. An agent that can see it already has your contact
  details doesn't ask for them; one that can see ``do_not_claim`` is empty knows to go get it.
* **Guardrails ride along in full.** Identity values and ``do_not_claim`` inject every turn
  because they're short and load-bearing. The long narrative sections inject as presence +
  size only, and the agent pulls them on demand — always-on context shouldn't scale with the
  length of someone's career.

**Where it lives.** ``<instance_root>/careercoach/`` — the host's per-instance plugin store
(``graph.sdk.plugin_store``, ADR 0004), so the dev sandbox, every fleet member and a container's
persisted volume each get their own, and ``scripts/dev-reset.sh`` wipes the sandbox's. Before
v0.7 it was ``~/.protoagent/careercoach[/<PROTOAGENT_INSTANCE>]``: a store found there is copied
forward once, the legacy folder gets a ``MIGRATED-TO`` note, and the old files are left in place
(for a rollback) but never adopted again — so wiping the new store really starts over.
``CAREERCOACH_DIR`` overrides the location (tests / power users) with its pre-0.7 layout.

The store helpers here (location, lock, atomic write, the unreadable-file guard) are shared with
``state.py``. No host imports at module level, so every function is unit-testable with nothing
but a temp dir (set ``CAREERCOACH_DIR``), and this file loads on its own, outside its package.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

try:  # POSIX advisory locking; absent on Windows, where only threads in one process serialize.
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX host
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("protoagent.plugins.careercoach")

PLUGIN_ID = "careercoach"

# Short scalar facts — injected verbatim every turn (cheap, and the ones most often re-asked).
IDENTITY_FIELDS: dict[str, str] = {
    "name": "Full name",
    "location": "Location",
    "work_auth": "Work authorization",
    "contact": "Contact (email · phone · LinkedIn · portfolio)",
    "headlines": "Role-type headlines they'd accept",
}

# Narrative sections — markdown. Injected as presence only; fetched in full when drafting.
SECTIONS: dict[str, str] = {
    "roles": "Roles and impact (most recent first)",
    "education": "Education, certifications, credentials",
    "skills": "Skills inventory, by honest level",
    "do_not_claim": "Lines never to claim on their behalf",
    "stories": "Story bank — pre-vetted STAR proof",
    "notes": "Notes for tailoring (targets, sensitivities, NDAs)",
}

# ``do_not_claim`` is a guardrail, not content: short, and worthless if the agent has to
# remember to go look it up. It injects in full alongside the identity scalars.
ALWAYS_INJECT_SECTIONS: tuple[str, ...] = ("do_not_claim",)

# Sections the agent can add to but never silently shrink: a write that would drop or reword an
# existing line is refused unless ``confirm_removal=True`` (the operator's explicit say-so). The
# profile's counterpart to ``packet.WRITABLE_SOURCES`` — the lines that bind the agent aren't the
# agent's to remove.
GUARDED_SECTIONS: tuple[str, ...] = ("do_not_claim",)

# How a write to a section lands. ``append`` (the default) merges in only the lines the section
# doesn't have yet, each after the line it follows in what was written — so recording roles one
# at a time keeps every one, and re-recording is a no-op. ``replace`` rewrites the section and is
# how a CORRECTION is made — read the section, fix it, write the whole thing back (appending a
# correction would leave the wrong line beside it). Identity fields are single values, set outright.
UPDATE_MODES: tuple[str, ...] = ("append", "replace")

TOTAL_FIELDS = len(IDENTITY_FIELDS) + len(SECTIONS)

# The voice/guardrail gate, injected with the profile on every turn.
#
# It lives here, not in a skill, because of an observed failure: the writing discipline sits in
# ``job-application-assistant/writing-style.md`` and only loads on the tailoring path, so a bare
# "convert my resume to a docx" produced a real deliverable at 16 em-dashes per 1000 words
# against the operator's stated limit of 3, carrying a phrase they had explicitly retired. A rule
# that only binds when you enter through the front door isn't a rule. Always-on is the fix.
WRITE_GATE = (
    "BEFORE WRITING ANYTHING IN THEIR NAME — CV, cover letter, application answer, bio, LinkedIn\n"
    "text, or any docx/pdf/html export — load the writing discipline first (`load_skill`\n"
    "job-application-assistant, then its writing-style.md; plus `my-writing-style` if\n"
    "`list_skills` shows one) and check your draft against DO NOT CLAIM above. This applies to a\n"
    'bare "make me a resume" exactly as much as to a tailored application: entering through a\n'
    "side door does not suspend the rules."
)

# Size caps, in characters. ``do_not_claim`` goes in front of the model IN FULL on every call, so
# it's held to what a list of hard stops needs; identity facts are one line each; the narrative
# sections are read whole before every draft. A write past a cap is refused with the reason, and
# ``context_block`` bounds whatever an older version may have stored. (The live jobCoach profile's
# largest values: identity 253, do_not_claim 251, roles 4,160.)
IDENTITY_LIMIT = 500
FIELD_LIMITS: dict[str, int] = {"do_not_claim": 3_000}
SECTION_LIMIT = 20_000
_IDENTITY_SHOWN = 300  # what the always-on block shows of one identity fact

# The files this store holds — the set copied forward from the pre-0.7 location.
STORE_FILES: tuple[str, ...] = ("profile.json", "applications.json")

# Written into a legacy folder once its store has been copied forward: which store(s) it went to
# and when. A destination listed here never adopts the legacy files again.
MIGRATED_MARKER = "MIGRATED-TO"


# ── where the store lives ─────────────────────────────────────────────────────────────
def _legacy_dir() -> Path:
    """The pre-0.7 location: ``$CAREERCOACH_DIR`` or ``~/.protoagent/careercoach``, plus a
    ``PROTOAGENT_INSTANCE`` subdir. Still the layout of an explicit ``CAREERCOACH_DIR``."""
    base = Path(os.environ.get("CAREERCOACH_DIR") or (Path.home() / ".protoagent" / "careercoach"))
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    return base / inst if inst else base


def _host_free_instance_root() -> Path:
    """The instance root as the host resolves it (``infra.paths._resolve_instance_paths``, ADR
    0004/0065), for when no host is importable — tests, or this file loaded on its own:
    ``PROTOAGENT_HOME`` is the root outright; else ``<box>/<PROTOAGENT_INSTANCE>``; else
    ``<box>/default``, where the box is ``PROTOAGENT_BOX_ROOT``, else ``/sandbox`` in a
    container, else ``~/.protoagent``. Keep it in step with the host."""
    home = os.environ.get("PROTOAGENT_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    raw_box = os.environ.get("PROTOAGENT_BOX_ROOT", "").strip()
    if raw_box:
        box = Path(raw_box).expanduser()
    else:
        box = Path("/sandbox") if Path("/sandbox").is_dir() else Path.home() / ".protoagent"
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        return box / (re.sub(r"[^A-Za-z0-9._-]", "_", inst) or "instance")
    return box / "default"


def _instance_store() -> Path:
    """``<instance_root>/careercoach`` through the host's own seam: ``graph.sdk.plugin_store``
    (protoAgent v0.148.0+), then ``infra.paths.instance_paths`` (older hosts), then the host-free
    mirror above. Imported lazily; any failure falls through, so a host-free run still resolves."""
    try:
        from graph.sdk import plugin_store

        return Path(plugin_store(plugin_id=PLUGIN_ID))
    except Exception:  # noqa: BLE001 — no host, an older host, or a stubbed seam
        pass
    try:
        from infra.paths import instance_paths

        return Path(instance_paths().store(PLUGIN_ID))
    except Exception:  # noqa: BLE001
        pass
    return _host_free_instance_root() / PLUGIN_ID


def _dir() -> Path:
    """The directory holding ``profile.json`` and ``applications.json`` (created on demand)."""
    store = _legacy_dir() if os.environ.get("CAREERCOACH_DIR", "").strip() else _instance_store()
    store.mkdir(parents=True, exist_ok=True)
    _adopt_legacy(store)
    return store


def _path() -> Path:
    return _dir() / "profile.json"


def backup_path(path) -> Path:
    """The store's previous version: every write first saves what it's about to replace to
    ``<file>.bak``, so the last change — a bad agent write included — can be undone by hand."""
    path = Path(path)
    return path.with_name(path.name + ".bak")


_ADOPTED: set[str] = set()
_ADOPT_GUARD = threading.Lock()


def _migrated_to(legacy: Path) -> set[str]:
    """The stores a legacy folder's ``MIGRATED-TO`` note says it has already been copied into."""
    try:
        lines = (legacy / MIGRATED_MARKER).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {ln.split("\t", 1)[-1].strip() for ln in lines if ln.strip() and not ln.startswith("#")}


def _adopt_legacy(store: Path) -> None:
    """Copy a store left at the pre-0.7 location forward — once, ever, per destination.

    Runs at most once per process per store (every read and write resolves the store through
    here first, so nothing can write the new file between the check and the copy). A file is only
    copied when the new one doesn't exist: the new location is authoritative from its first write.
    Afterwards the legacy folder gets a ``MIGRATED-TO`` note naming this store, and a store named
    there never adopts the legacy files again — so ``scripts/dev-reset.sh``, or an operator
    deleting a member's ``careercoach/`` folder to start over, isn't undone by the next start. The
    legacy files themselves are never modified or deleted (a rollback to 0.6 still finds them).
    Serialized in-process and, on POSIX, across processes."""
    key = str(store)
    if key in _ADOPTED:
        return
    with _ADOPT_GUARD:
        if key in _ADOPTED:
            return
        legacy = _legacy_dir()
        if legacy.is_dir() and legacy.resolve() != store.resolve():
            dest = str(store.resolve())
            with _file_lock(store / ".migrate.lock"):
                if dest not in _migrated_to(legacy):
                    present = [name for name in STORE_FILES if (legacy / name).is_file()]
                    failed = False
                    for name in present:
                        src, dst = legacy / name, store / name
                        if dst.exists():
                            continue
                        try:
                            fd, tmp = tempfile.mkstemp(dir=str(store), prefix=f".{name}.", suffix=".adopt")
                            os.close(fd)
                            shutil.copy2(src, tmp)
                            os.replace(tmp, dst)
                            log.info(
                                "[careercoach] adopted %s from its pre-0.7 location: %s -> %s",
                                name,
                                src,
                                dst,
                            )
                        except OSError as exc:
                            failed = True
                            log.warning("[careercoach] could not adopt %s from %s: %s", name, src, exc)
                    if present and not failed:
                        _note_migration(legacy, dest)
        _ADOPTED.add(key)


def _note_migration(legacy: Path, dest: str) -> None:
    marker = legacy / MIGRATED_MARKER
    try:
        with open(marker, "a", encoding="utf-8") as fh:
            if fh.tell() == 0:
                fh.write(
                    "# Career Coach 0.7+ copied this folder's profile.json / applications.json to the\n"
                    "# store(s) below and no longer reads them here. Kept for a rollback; safe to delete.\n"
                )
            fh.write(f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}\t{dest}\n")
    except OSError as exc:  # a missing note only means a later wipe could re-adopt; never fail a read
        log.warning("[careercoach] could not write %s: %s", marker, exc)


# ── serialized, atomic, never-over-an-unreadable-file writes ──────────────────────────
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _file_lock(lock_path: Path):
    """Exclusive lock on ``lock_path``: a ``threading.Lock`` for this process's threads, plus a
    POSIX ``flock`` for other processes. Without ``fcntl`` (Windows) only the thread lock is taken
    — parallel tool calls in one process still serialize, but two processes sharing one store do
    not. Not re-entrant."""
    with _thread_lock(str(lock_path)):
        if fcntl is None:
            yield
            return
        with open(lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def _locked(name: str = "profile"):
    """Serialize read-modify-write on one store file (``profile`` or ``applications``).

    Models emit tool calls in PARALLEL, so several ``update_field`` calls can be in flight at
    once. Without this each one loads, edits its own field and saves the whole object, so the
    last writer wins and every other field's edit is silently lost. Observed in the wild: a run
    that recorded name, location, headlines and skills ended up with only the last two."""
    with _file_lock(_dir() / f".{name}.lock"):
        yield


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + rename, so the store is never observed half-written.

    ``write_text`` truncates then writes, which leaves a window where a concurrent writer can
    interleave and a shorter write can strand the tail of a longer one — producing a file that is
    valid JSON followed by garbage. ``os.replace`` is atomic within a filesystem, so a reader
    sees either the old file or the new one, never a splice of both."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_store(path: Path, text: str) -> None:
    """Persist a store file, first saving the version it replaces to ``backup_path`` — one
    generation back, so the change being made can always be undone. Callers hold the lock and
    have already refused an unreadable file, so what's saved is a good version."""
    try:
        previous = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        previous = None
    if previous is not None and previous.strip() and previous != text:
        try:
            _atomic_write(backup_path(path), previous)
        except OSError as exc:  # the backup is a belt; a failed one must not fail the write
            log.warning("[careercoach] could not save %s: %s", backup_path(path), exc)
    _atomic_write(path, text)


class StoreUnreadable(RuntimeError):
    """A store file has content that can't be parsed. Writers raise this rather than treat it as
    empty: an ordinary one-field update would otherwise save that one field over everything."""

    def __init__(self, path, reason: str):
        self.path = Path(path)
        self.reason = reason
        backup = backup_path(self.path)
        remedy = (
            f"Fix the file (usually a one-character slip), or restore {backup}, the version before "
            "the coach's last change."
            if backup.is_file()
            else "Fix the file to continue."
        )
        super().__init__(
            f"{self.path.name} at {self.path} is unreadable ({reason}), so nothing was saved — "
            f"a write now would replace everything in it. {remedy}"
        )


def _read_json(path: Path):
    """``(data, error)``: ``(None, "")`` when the file doesn't exist or is empty (nothing to lose),
    ``(None, reason)`` when it has content that can't be read or parsed."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as exc:
        return None, f"can't be read: {exc}"
    if not text.strip():
        return None, ""
    try:
        return json.loads(text), ""
    except ValueError as exc:
        return None, f"not valid JSON: {exc}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ── the profile ───────────────────────────────────────────────────────────────────────
def empty_profile() -> dict:
    """A well-formed, entirely unfilled profile — the shape every reader can rely on."""
    return {
        "identity": {k: "" for k in IDENTITY_FIELDS},
        "sections": {k: "" for k in SECTIONS},
        "updated": "",
    }


def _obj(value: object) -> dict:
    """A nested JSON object, or ``{}`` for anything that isn't one. ``(x or {})`` isn't enough: a
    truthy non-dict (``{"identity": "corrupted"}``) sails through it and then raises AttributeError
    on ``.get`` — a damaged file must read as empty, never break a turn."""
    return value if isinstance(value, dict) else {}


def _shape_error(raw: object) -> str:
    """Why this parsed JSON can't be read as a profile, or ``""``.

    A nested value that isn't an object holds data this code can't interpret. Normalizing it away
    would read to the operator as "nothing recorded yet" and the next write would erase it, so the
    store counts as unreadable: reported to the agent, and writes refused."""
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        return "not a JSON object"
    for key in ("identity", "sections"):
        value = raw.get(key)
        if value is not None and not isinstance(value, dict):
            return f"{key!r} is not a JSON object"
    return ""


def _normalize(raw: dict) -> dict:
    prof = empty_profile()
    for k in IDENTITY_FIELDS:
        v = _obj(raw.get("identity")).get(k, "")
        prof["identity"][k] = str(v).strip() if v is not None else ""
    for k in SECTIONS:
        v = _obj(raw.get("sections")).get(k, "")
        prof["sections"][k] = str(v).strip() if v is not None else ""
    prof["updated"] = str(raw.get("updated", "") or "")
    return prof


def load_profile_checked() -> tuple[dict, str]:
    """``(profile, error)``. ``error`` is "" when the store is fine, absent or empty, else why it's
    unreadable — in which case ``profile`` is empty. Readers that can say so to the agent use this."""
    path = _path()
    raw, err = _read_json(path)
    err = err or _shape_error(raw)
    if err:
        # Degrade rather than raise — an unreadable profile must never break a turn. But say so
        # LOUDLY: silence here once masked a torn file as "nothing recorded yet", which reads to
        # the operator as the feature not working rather than their data being damaged.
        log.warning("[careercoach] profile at %s is unreadable (%s) — reading as empty, refusing writes", path, err)
        return empty_profile(), err
    return (_normalize(raw) if raw else empty_profile()), ""


def load_profile() -> dict:
    """The stored profile, normalized. A missing or unreadable file reads as empty rather than
    raising (so a turn never breaks) — but ``update_field`` refuses to write over an unreadable
    one, and ``load_profile_checked`` reports why it's unreadable."""
    return load_profile_checked()[0]


def save_profile(profile: dict) -> dict:
    """Persist a whole profile (normalized + stamped). Returns what was written. A whole-object
    overwrite: callers go through ``update_field``, which holds the lock and checks the store."""
    out = empty_profile()
    for k in IDENTITY_FIELDS:
        out["identity"][k] = str(_obj(profile.get("identity")).get(k, "") or "").strip()
    for k in SECTIONS:
        out["sections"][k] = str(_obj(profile.get("sections")).get(k, "") or "").strip()
    out["updated"] = _now()
    _write_store(_path(), json.dumps(out, indent=2) + "\n")
    return out


def _limit(field: str) -> int:
    return IDENTITY_LIMIT if field in IDENTITY_FIELDS else FIELD_LIMITS.get(field, SECTION_LIMIT)


_BULLET = re.compile(r"^[-*+]\s+")


def _norm(line: str) -> str:
    """A line for comparison: whitespace collapsed and any bullet marker spelled ``- ``, so the same
    fact typed with ``*`` in one editor and ``-`` in another is still the same fact."""
    return _BULLET.sub("- ", " ".join((line or "").split()))


def _lines(text: str) -> list[str]:
    return [n for n in (_norm(ln) for ln in (text or "").splitlines()) if n]


def _keys(lines: list[str]) -> list:
    """Each line's identity for merging: a heading is itself; any other line is (the heading it
    sits under, the line) — so "- Python" under two different roles is two different lines."""
    ctx, out = "", []
    for ln in lines:
        s = _norm(ln)
        if not s:
            out.append(None)
        elif s.startswith("#"):
            ctx = s
            out.append(("", s))
        else:
            out.append((ctx, s))
    return out


def _merge(existing: str, addition: str) -> tuple[str, list[str]]:
    """``addition`` merged into a section line by line: ``(merged_text, lines_added)``.

    Only lines ``existing`` doesn't already have are added, each right after the line it follows
    in ``addition`` — so a bullet added under one role lands under that role, a role added at the
    top lands at the top, and re-recording what's already there adds nothing. ``lines_added`` is
    exactly what the merge wrote, which is what an import's preview reports."""
    add_lines = (addition or "").strip().splitlines()
    if not add_lines:
        return existing, []
    if not (existing or "").strip():
        return "\n".join(add_lines), [ln for ln in add_lines if ln.strip()]
    out = existing.splitlines()
    keys = _keys(out)
    have = {k for k in keys if k}
    added: list[str] = []
    pending: list[tuple] = []  # new lines seen before the first line `existing` already has
    cursor: int | None = None
    for key, line in zip(_keys(add_lines), add_lines):
        if key is None:
            continue
        if key in have:
            start = 0 if cursor is None else cursor + 1
            idx = next((i for i in range(start, len(keys)) if keys[i] == key), None)
            if idx is None:
                idx = next((i for i, k in enumerate(keys) if k == key), None)
            if idx is None:
                continue  # a repeat of a line still waiting to be placed
            if pending:
                block = pending + ([(None, "")] if key[0] == "" else [])
                out[idx:idx] = [ln for _, ln in block]
                keys[idx:idx] = [k for k, _ in block]
                idx += len(block)
                pending = []
            cursor = idx
            continue
        have.add(key)
        added.append(line)
        if cursor is None:
            pending.append((key, line))
            continue
        block = [(None, ""), (key, line)] if key[0] == "" and out[cursor].strip() else [(key, line)]
        out[cursor + 1 : cursor + 1] = [ln for _, ln in block]
        keys[cursor + 1 : cursor + 1] = [k for k, _ in block]
        cursor += len(block)
    if pending:
        out += ([""] if out and out[-1].strip() else []) + [ln for _, ln in pending]
    return "\n".join(out).strip(), added


def _same(a: str, b: str) -> bool:
    """Equal as text, ignoring trailing spaces and runs of blank lines."""

    def canon(t: str) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in (t or "").splitlines())).strip()

    return canon(a) == canon(b)


def _dropped(old: str, new: str) -> list[str]:
    kept = set(_lines(new))
    return [ln for ln in _lines(old) if ln not in kept]


def _refuse_removal(field: str, dropped: list[str]) -> PermissionError:
    listed = "\n".join(f"  - {ln}" for ln in dropped)
    return PermissionError(
        f"Refused: this would remove {len(dropped)} line(s) from {field}, the hard stops "
        f"the operator set:\n{listed}\nThose are theirs to drop, not yours. Ask them; only "
        "if they explicitly confirm, call again with confirm_removal=true. To add a line, "
        "use mode='append' (the default)."
    )


def _too_long(field: str, size: int) -> ValueError:
    if field in ALWAYS_INJECT_SECTIONS:
        why = "it goes in front of the model in full on every call"
    elif field in SECTIONS:
        why = "the whole record is read before every draft"
    else:
        why = "it's a single fact shown on every call"
    return ValueError(
        f"Refused: {field} would be {size:,} characters; the cap is {_limit(field):,} because {why}. "
        "Nothing was saved — keep what matters and ask the operator to trim the rest."
    )


def _plan(prof: dict, field: str, text: str, mode: str, confirm_removal: bool) -> dict:
    """What writing ``text`` to ``field`` would do to ``prof`` — the one place the write rules live,
    used by ``update_field`` and by the import alike. Returns ``{"field", "outcome", "old", "new",
    "added", "dropped", "error"}``; ``outcome`` is set · appended · replaced · unchanged · refused
    (a hard stop would go) · too long (past the cap), and ``error`` is what to raise for the last two."""
    slot = "identity" if field in IDENTITY_FIELDS else "sections"
    old = prof[slot][field]
    if slot == "identity" or mode == "replace":
        have = set(_lines(old))
        new, added = text, [ln for ln in text.splitlines() if _norm(ln) and _norm(ln) not in have]
    else:
        new, added = _merge(old, text)
    row = {"field": field, "old": old, "new": new, "added": added, "dropped": _dropped(old, new), "error": None}
    if _same(new, old):
        return {**row, "outcome": "unchanged", "new": old, "added": [], "dropped": []}
    if len(new) > _limit(field):
        return {**row, "outcome": "too long", "error": _too_long(field, len(new))}
    if field in GUARDED_SECTIONS and row["dropped"] and not confirm_removal:
        return {**row, "outcome": "refused", "error": _refuse_removal(field, row["dropped"])}
    outcome = "set" if not old else ("appended" if slot == "sections" and mode == "append" else "replaced")
    return {**row, "outcome": outcome}


def _load_strict() -> dict:
    """The stored profile for a writer or planner: an unreadable store raises ``StoreUnreadable``."""
    path = _path()
    raw, err = _read_json(path)
    err = err or _shape_error(raw)
    if err:
        raise StoreUnreadable(path, err)
    return _normalize(raw) if raw else empty_profile()


def update_field(field: str, content: str, *, mode: str = "append", confirm_removal: bool = False) -> dict:
    """Record one identity field or one section, leaving every other field untouched.

    Field-at-a-time is the point: a long onboarding interview saves as it goes, so nothing is lost
    if it's abandoned. Identity fields are single values and are set outright (for the multi-part
    ones — ``contact``, ``headlines`` — pass the whole line). Sections **append** by default: only
    the lines not already there are merged in, each under the line it follows in ``content`` — so
    recording roles one at a time keeps every role, and no ordinary write truncates a career
    history. ``mode="replace"`` rewrites a section: it's how a correction is made, by a caller that
    has just read the section in full and passes the whole fixed text.

    Refuses rather than guesses:

    * an unknown field → ``KeyError``; an unknown mode, or a value past its size cap → ``ValueError``;
    * an unreadable store → ``StoreUnreadable`` (writing one field would erase the rest);
    * dropping or rewording any line of a ``GUARDED_SECTIONS`` section without
      ``confirm_removal=True`` → ``PermissionError``.

    Returns the stored profile plus ``"change"``: ``set`` · ``appended`` · ``replaced`` ·
    ``unchanged`` (nothing written)."""
    if field not in IDENTITY_FIELDS and field not in SECTIONS:
        known = ", ".join(list(IDENTITY_FIELDS) + list(SECTIONS))
        raise KeyError(f"unknown profile field {field!r}; known: {known}")
    if mode not in UPDATE_MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of: {', '.join(UPDATE_MODES)}")
    text = (content or "").strip()
    # Load → plan → save under one lock. Parallel tool calls make this a genuine race, not a
    # theoretical one: without it, concurrent field writes silently drop each other's edits.
    with _locked():
        prof = _load_strict()
        row = _plan(prof, field, text, mode, confirm_removal)
        if row["error"]:
            raise row["error"]
        if row["outcome"] == "unchanged":
            return {**prof, "change": "unchanged"}
        prof["identity" if field in IDENTITY_FIELDS else "sections"][field] = row["new"]
        return {**save_profile(prof), "change": row["outcome"]}


def completeness(profile: dict | None = None) -> dict:
    """What's known, what isn't, and how much of the picture exists.

    This is the value that stops the agent re-interviewing: ``known`` names the fields it must
    never ask for again, ``missing`` names the only ones still worth asking about."""
    prof = profile if profile is not None else load_profile()
    known: list[str] = []
    missing: list[str] = []
    for k in IDENTITY_FIELDS:
        (known if prof["identity"].get(k) else missing).append(k)
    for k in SECTIONS:
        (known if prof["sections"].get(k) else missing).append(k)
    return {
        "known": known,
        "missing": missing,
        "filled": len(known),
        "total": TOTAL_FIELDS,
        "empty": len(known) == 0,
        "updated": prof.get("updated", ""),
    }


def store_signature():
    """A cheap change-detector for the profile file (``None`` when absent) — lets the middleware
    re-read only when something was actually written."""
    try:
        st = _path().stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _summarize(text: str) -> str:
    """A one-line stand-in for a narrative section: enough to prove it exists and hint at
    its weight, without spending always-on context on the body."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return f"recorded ({len(lines)} lines, {len(text)} chars)"


# The compatibility forms of the delimiter characters (fullwidth / small-form < > /) — the only
# code points whose NFKC form is one of them — folded to ASCII before neutralizing, rather than
# NFKC-normalizing the whole value (which would also turn "10⁶" into "106" in a hard stop).
_DELIMITER_LOOKALIKES = str.maketrans({"﹤": "<", "＜": "<", "﹥": ">", "＞": ">", "／": "/"})
# Anything shaped like a tag opener — ``<operator_profile>``, ``</injected_context>``, ``<system>``.
# (No two adjacent ambiguous whitespace runs, so a "<" before a long run of spaces stays linear.)
_TAG_OPENER = re.compile(r"<(?=\s*(?:/\s*)?[A-Za-z_!?])")


def _defang(text: str) -> str:
    """Neutralize tag-shaped text in a profile value, so nothing the agent recorded — say, a line
    harvested from an ingested CV — can close the ``<operator_profile>`` block (or the host frame
    around it) and speak as the block itself. Invisible format characters (zero-width space,
    joiners, direction overrides) are dropped first, and look-alike brackets folded to ASCII, so
    ``<`` + ZWSP + ``/operator_profile>`` or a fullwidth ``＜/operator_profile＞`` is caught too."""
    text = "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")
    return _TAG_OPENER.sub("&lt;", text.translate(_DELIMITER_LOOKALIKES))


def _one_line(text: str, limit: int = _IDENTITY_SHOWN) -> str:
    """An identity value on one line, clipped: a scalar fact has no business adding lines (or pages)
    to the block."""
    flat = " ".join(_defang(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def unreadable_block(reason: str) -> str:
    """What the model is told when ``profile.json`` exists but can't be parsed — so it neither
    re-interviews for data still on disk nor drafts without the operator's hard stops."""
    path = _path()
    lines = [
        "<operator_profile>",
        _defang(f"Your operator's profile file is unreadable ({reason}): {path}"),
        "Nothing in it reaches you this turn, you don't have their DO NOT CLAIM hard stops, and",
        "nothing you record will save until it's fixed. Tell the operator. Don't re-interview them",
        "for what it held (it's still on disk), and don't draft in their name until it's readable.",
    ]
    backup = backup_path(path)
    if backup.is_file():
        lines.append(_defang(f"The version before your last change is {backup}, if fixing the file is harder."))
    lines.append("</operator_profile>")
    return "\n".join(lines)


def context_block(profile: dict | None = None) -> str:
    """The ``<operator_profile>`` block put in front of every model call.

    Deliberately framed as first-party state rather than recalled memory: this is the operator's
    own verified record, maintained by the agent, and it should carry more authority than a RAG
    hit. Returns "" when nothing is known — an empty block is noise, and the onboarding skill
    handles the cold-start case on its own. Values are rendered through ``_defang`` (identity on
    one line, ``do_not_claim`` indented), so recorded text can't break out of the block."""
    if profile is None:
        prof, err = load_profile_checked()
        if err:
            return unreadable_block(err)
    else:
        prof = profile
    cov = completeness(prof)
    if cov["empty"]:
        return ""

    lines = [
        "<operator_profile>",
        "Your operator's verified profile — first-party state you maintain, not recalled memory.",
        "NEVER ask for anything listed under KNOWN; you already have it. Ask only about MISSING,",
        "and only when the task at hand actually needs it. Use the profile tools to read a full",
        "section before drafting; what follows is the index, not the content.",
        "",
        "KNOWN:",
    ]
    for k in IDENTITY_FIELDS:
        v = prof["identity"].get(k)
        if v:
            lines.append(f"  {k}: {_one_line(v)}")
    for k in SECTIONS:
        v = prof["sections"].get(k)
        if v and k not in ALWAYS_INJECT_SECTIONS:
            lines.append(f"  {k}: {_summarize(v)}")

    if cov["missing"]:
        lines.append("")
        lines.append("MISSING (not yet recorded): " + ", ".join(cov["missing"]))
    lines.append("")
    lines.append(
        f"COVERAGE: {cov['filled']} of {cov['total']} fields"
        + (f", last updated {cov['updated']}" if cov["updated"] else "")
    )

    for k in ALWAYS_INJECT_SECTIONS:
        body = prof["sections"].get(k)
        if body:
            cap = _limit(k)
            shown = body if len(body) <= cap else body[:cap].rsplit("\n", 1)[0]
            lines.append("")
            lines.append(f"{SECTIONS[k].upper()} — hard stops when writing in their name:")
            lines += [f"  {ln}" if ln.strip() else "" for ln in _defang(shown).splitlines()]
            if len(shown) < len(body):  # stored by an older version, before the cap existed
                lines.append(
                    f"  … {len(body) - len(shown):,} more characters: read them with "
                    f"careercoach_get_profile('{k}'), and ask the operator to trim this list."
                )

    lines += ["", WRITE_GATE, "</operator_profile>"]
    return "\n".join(lines)


def render_markdown(profile: dict | None = None) -> str:
    """The profile as markdown — what ``careercoach_read_profile("experience")`` serves, and the
    body of the export. Same section labels ``parse_experience`` reads back."""
    prof = profile if profile is not None else load_profile()
    ident, sect = prof["identity"], prof["sections"]
    out = ["## Identity"]
    for k, label in IDENTITY_FIELDS.items():
        out.append(f"- **{label}:** {ident.get(k) or '_(not recorded)_'}")
    for k, label in SECTIONS.items():
        out += ["", f"## {label}", "", sect.get(k) or "_(not recorded)_"]
    return "\n".join(out) + "\n"


def to_markdown(profile: dict | None = None) -> str:
    """The portable export: ``render_markdown`` under a title and a banner, written to
    ``Resume/Experience (profile export).md`` — never over the operator's own ``Experience.md``.
    Readable, diffable, and handable to anyone, including a different agent."""
    prof = profile if profile is not None else load_profile()
    head = [
        "# Experience (profile export)",
        "",
        "> Generated from the Career Coach operator profile"
        + (f" on {prof.get('updated')}" if prof.get("updated") else "")
        + ".",
        "> A regenerated snapshot: the next export overwrites this file. To change the record, tell",
        "> your coach in chat (the Career Coach panel is a read-only view of it).",
        "",
    ]
    return "\n".join(head) + render_markdown(prof)


# ── Experience.md → profile: the explicit import ──────────────────────────────────────
# An H2 heading in the operator's file → the profile field it feeds. First match wins, so the
# specific patterns come first ("Lines never to claim" is do_not_claim, not roles). Matched on the
# first 200 characters of a heading only.
_HEADING_FIELDS: tuple[tuple[str, re.Pattern], ...] = (
    ("do_not_claim", re.compile(r"\b(?:do not|don't|never)\b[^\n]{0,120}?\bclaim", re.I)),
    ("stories", re.compile(r"\bstor(?:y|ies)\b|\bstar\b", re.I)),
    ("education", re.compile(r"\beducation\b|\bcertific|\bcredential|\bdegree|\bqualification", re.I)),
    ("skills", re.compile(r"\bskill", re.I)),
    ("notes", re.compile(r"\bnotes?\b", re.I)),
    ("roles", re.compile(r"\broles?\b|\bexperience\b|\bemployment\b|\bwork history\b|\bcareer\b|\bpositions?\b", re.I)),
    ("identity", re.compile(r"\bidentity\b|\bcontact\b|\babout\b|\bpersonal\b", re.I)),
)
# A "- **Label:** value" / "Label: value" line in the identity section → the identity field(s).
_IDENTITY_LABELS: tuple[tuple[str, re.Pattern], ...] = (
    ("name", re.compile(r"\bname\b", re.I)),
    ("location", re.compile(r"\blocation\b|\bbased\b|\bcity\b", re.I)),
    ("work_auth", re.compile(r"\bauthori[sz]ation\b|\bvisa\b|\bright to work\b|\bwork auth", re.I)),
    ("contact", re.compile(r"\bcontact\b|\bemail\b|\bphone\b|\blinkedin\b|\bportfolio\b|\bgithub\b", re.I)),
    ("headlines", re.compile(r"\bheadline|\btitles?\b|\brole-type\b", re.I)),
)
_LABELLED = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*(?P<b>[^*]+?):?\*\*:?|(?P<p>[A-Za-z][^:*]{0,48}):)\s*(?P<value>.*)$")
_NOT_RECORDED = "_(not recorded)_"
# The export's own headings: these always map to their field, whatever the file is.
_EXPORT_HEADINGS: dict[str, str] = {"identity": "identity", **{label.lower(): f for f, label in SECTIONS.items()}}
# How a copy of the export announces itself (``to_markdown``, and the pre-0.7 export too). This is
# NOT the retired "who wins" sniffing: it decides only that the shipped template's hint text can't
# be in play (every line came from the profile) and that a heading the export didn't write is body
# text — the two things that made export → import lose or move content.
EXPORT_TITLE = "# Experience (profile export)"
EXPORT_BANNER = "> Generated from the Career Coach operator profile"
# Template lines from before 0.7 split these slots — still hint text in an older seeded copy.
LEGACY_TEMPLATE_LINES: tuple[str, ...] = (
    "- **Location / work authorization:**",
    "- **Explicitly do NOT claim:** _(guards against overreach in tailoring)_",
)


def _h2(line: str) -> str | None:
    """The text of a level-2 markdown heading, else ``None``. Plain string work, no regex: a
    heading with a long whitespace run once took seconds to parse (catastrophic backtracking)."""
    if not line.startswith("##") or line.startswith("###") or line[2:3] not in (" ", "\t"):
        return None
    return line[2:].strip().rstrip("#").strip()


def _heading_field(heading: str, export_shaped: bool) -> str | None:
    field = _EXPORT_HEADINGS.get(heading.lower())
    if field or export_shaped:
        return field
    head = heading[:200]
    return next((f for f, pat in _HEADING_FIELDS if pat.search(head)), None)


def _labelled(line: str) -> tuple[str, str] | None:
    m = _LABELLED.match(line)
    if not m:
        return None
    return (m.group("b") or m.group("p") or "").strip(), m.group("value").strip()


def _split_location(value: str) -> tuple[str, str]:
    """A combined "Location / work authorization" value → (location, work_auth). Split on the first
    separator; with none, the one value answers both (better than the agent asking again)."""
    for sep in (" · ", ";", " | ", " / ", " — ", " - "):
        left, _, right = value.partition(sep)
        if left.strip() and right.strip():
            return left.strip(), right.strip()
    return value, value


def parse_experience(text: str, template_text: str = "") -> tuple[dict[str, str], list[str], list[str]]:
    """Read an operator-written ``Experience.md`` into profile fields: ``(fields, unmapped, notes)``.

    Sections are found by their ``##`` heading — an export's own labels, else the template's or a
    plain "Skills" / "Experience" — and a ``##`` that matches no field stays part of the section it
    sits in (``notes`` says so). Identity facts come from ``Label: value`` lines; the template's
    combined "Location / work authorization" line feeds both fields. Every line that's still the
    shipped template's hint text is dropped (not in an export, whose lines all came from the
    profile), as are ``_(not recorded)_`` placeholders and the preamble. Anything that can't be
    placed comes back in ``unmapped`` for the agent to raise, rather than being guessed into a field.
    Nothing is moved between sections: a "do NOT claim" line under skills stays in skills."""
    lines = (text or "").splitlines()
    head = [ln.strip() for ln in lines[:8]]
    export_shaped = EXPORT_TITLE in head or any(ln.startswith(EXPORT_BANNER) for ln in head)
    template = (
        set()
        if export_shaped
        else {ln.strip() for ln in (template_text or "").splitlines() if ln.strip()} | set(LEGACY_TEMPLATE_LINES)
    )
    fields: dict[str, list[str]] = {}
    unmapped: list[str] = []
    notes: list[str] = []
    heading, target = "", None  # None = preamble; "" = a section with no matching field

    def keep(line: str) -> bool:
        s = line.strip()
        return bool(s) and s not in template and s != _NOT_RECORDED

    for raw in lines:
        line = raw.rstrip()
        h2 = _h2(line)
        if h2 is not None:
            field = _heading_field(h2, export_shaped)
            if field is None and target in SECTIONS:
                fields.setdefault(target, []).append(line)
                if not export_shaped:
                    notes.append(f"'## {h2[:80]}' matches no profile field, so it stays part of {target}")
                continue
            heading, target = h2, (field or "")
            if target in SECTIONS and fields.get(target):
                fields[target].append("")  # a second heading feeding the same field starts a new paragraph
            continue
        if target is None:  # preamble: the title and a banner are the file's, not the operator's history
            if keep(line) and not line.lstrip().startswith(("#", ">")):
                unmapped.append(line.strip())
            continue
        if not keep(line):
            if not line.strip() and target in SECTIONS and fields.get(target):
                fields[target].append("")  # keep paragraph breaks inside a section
            continue
        if target == "identity":
            pair = _labelled(line)
            value = pair[1] if pair and pair[1] != _NOT_RECORDED else ""
            if pair and not value:
                continue  # "Label:" with nothing after it is an unfilled slot, not content
            label = pair[0][:120] if pair else ""
            matched = [f for f, pat in _IDENTITY_LABELS if pair and pat.search(label)]
            if "location" in matched and "work_auth" in matched:
                location, work_auth = _split_location(value)
                fields.setdefault("location", []).append(location)
                fields.setdefault("work_auth", []).append(work_auth)
            elif matched:
                fields.setdefault(matched[0], []).append(value)
            else:
                unmapped.append(f"{heading}: {line.strip()}")
            continue
        if target == "":
            unmapped.append(f"{heading}: {line.strip()}")
            continue
        if target == "skills" and not export_shaped:
            pair = _labelled(line)
            if pair and _HEADING_FIELDS[0][1].search(pair[0][:120]):
                notes.append(
                    "skills has a line labelled 'do NOT claim'; it stays in skills — if it's a hard stop, "
                    "ask the operator and record it in do_not_claim"
                )
        fields.setdefault(target, []).append(line)

    out: dict[str, str] = {}
    for field, got in fields.items():
        if field in IDENTITY_FIELDS:
            out[field] = " · ".join(dict.fromkeys(got))
        else:
            body = re.sub(r"\n{3,}", "\n\n", "\n".join(got)).strip()
            if body:
                out[field] = body
    return out, unmapped, notes


class ImportChanged(ValueError):
    """``apply_import`` was handed a preview that no longer matches what the import would do."""


def _import_rows(prof: dict, fields: dict[str, str], mode: str, confirm_removal: bool) -> list[dict]:
    rows: list[dict] = []
    for field in list(IDENTITY_FIELDS) + list(SECTIONS):
        text = (fields.get(field) or "").strip()
        if not text:
            continue
        old = prof["identity"].get(field, "") if field in IDENTITY_FIELDS else ""
        if old and mode == "append" and not _same(old, text):
            # Append never overwrites an identity fact the profile already holds.
            rows.append(
                {
                    "field": field,
                    "outcome": "kept",
                    "old": old,
                    "new": old,
                    "added": [],
                    "dropped": [],
                    "error": None,
                    "file": text,
                }
            )
            continue
        rows.append(_plan(prof, field, text, mode, confirm_removal))
    return rows


def _preview_id(source: str, mode: str, confirm_removal: bool, rows: list[dict]) -> str:
    """Binds an apply to the preview the operator saw: the file's content, the options, and every
    field's exact outcome and resulting value."""
    payload = json.dumps([source, mode, bool(confirm_removal), [[r["field"], r["outcome"], r["new"]] for r in rows]])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def plan_import(
    fields: dict[str, str], *, mode: str = "append", confirm_removal: bool = False, source: str = ""
) -> tuple[list[dict], str]:
    """What importing ``fields`` would do, field by field, plus the ``preview_id`` that
    ``apply_import`` needs to do exactly that. Writes nothing. ``source`` identifies the file's
    content (a hash), so an edit to the file after the preview invalidates it."""
    if mode not in UPDATE_MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of: {', '.join(UPDATE_MODES)}")
    rows = _import_rows(_load_strict(), fields, mode, confirm_removal)
    return rows, _preview_id(source, mode, confirm_removal, rows)


def apply_import(
    fields: dict[str, str], *, preview_id: str, mode: str = "append", confirm_removal: bool = False, source: str = ""
) -> list[dict]:
    """Do exactly what the preview ``preview_id`` showed — or nothing. Re-plans under the profile
    lock and raises ``ImportChanged`` if the file, the profile, ``mode`` or ``confirm_removal`` no
    longer give the same result; otherwise writes every planned field in ONE save, so the ``.bak``
    it leaves is the whole pre-import profile and one restore undoes the import."""
    if mode not in UPDATE_MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of: {', '.join(UPDATE_MODES)}")
    with _locked():
        prof = _load_strict()
        rows = _import_rows(prof, fields, mode, confirm_removal)
        if not preview_id or _preview_id(source, mode, confirm_removal, rows) != preview_id:
            raise ImportChanged(
                "the file, the profile, mode or confirm_removal changed since that preview, so nothing was "
                "written — preview again and show the operator what it would do now."
            )
        writes = [r for r in rows if r["outcome"] in ("set", "appended", "replaced")]
        for r in writes:
            prof["identity" if r["field"] in IDENTITY_FIELDS else "sections"][r["field"]] = r["new"]
        if writes:
            save_profile(prof)
        return rows
