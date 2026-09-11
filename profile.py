"""Operator profile — structured, instance-scoped, always-injected, host-free.

The coach's answer to "who am I working for?". Before this existed the answer lived in a
markdown file the operator had to find in Finder, which meant two failures: the agent
re-interviewed for facts it already held (it had no cheap way to know what it knew), and the
person being described couldn't see the record. So the profile is now *state*, not a document.

**The profile is the single source of truth.** Everything that reads the operator's history —
the always-on block, ``careercoach_read_profile("experience")``, the drafting and scoring crew —
reads *this*. A workspace ``Resume/Experience.md`` is never a competing read source; it takes part
in two explicit, one-way moves:

  ``Experience.md``  --import (``parse_experience`` + ``import_fields``, on request)-->  ``profile.json``
  ``profile.json``   --export (``to_markdown``)-->  ``Resume/Experience (profile export).md``

and every other write goes through ``update_field``, so an import obeys the same append/replace
and ``do_not_claim`` rules as the agent does.

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

# How a write to a section lands. ``append`` (the default) adds to what's there, so recording
# roles one at a time keeps every one; ``replace`` rewrites the section and is how a CORRECTION is
# made — read the section, fix it, write the whole thing back (appending a correction would leave
# the wrong line beside it). Identity fields are single values and are always set outright.
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


def _normalize(raw: dict) -> dict:
    prof = empty_profile()
    for k in IDENTITY_FIELDS:
        v = (raw.get("identity") or {}).get(k, "")
        prof["identity"][k] = str(v).strip() if v is not None else ""
    for k in SECTIONS:
        v = (raw.get("sections") or {}).get(k, "")
        prof["sections"][k] = str(v).strip() if v is not None else ""
    prof["updated"] = str(raw.get("updated", "") or "")
    return prof


def load_profile_checked() -> tuple[dict, str]:
    """``(profile, error)``. ``error`` is "" when the store is fine, absent or empty, else why it's
    unreadable — in which case ``profile`` is empty. Readers that can say so to the agent use this."""
    path = _path()
    raw, err = _read_json(path)
    if not err and raw is not None and not isinstance(raw, dict):
        err = "not a JSON object"
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
        out["identity"][k] = str((profile.get("identity") or {}).get(k, "") or "").strip()
    for k in SECTIONS:
        out["sections"][k] = str((profile.get("sections") or {}).get(k, "") or "").strip()
    out["updated"] = _now()
    _write_store(_path(), json.dumps(out, indent=2) + "\n")
    return out


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _appended(existing: str, addition: str) -> str:
    """``addition`` added to a section as a new paragraph — unless every line of it is already
    there (re-recording a settled fact is a no-op, not a duplicate)."""
    if not addition:
        return existing
    if not existing:
        return addition
    have = set(_lines(existing))
    if all(ln in have for ln in _lines(addition)):
        return existing
    return f"{existing}\n\n{addition}"


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


def update_field(field: str, content: str, *, mode: str = "append", confirm_removal: bool = False) -> dict:
    """Record one identity field or one section, leaving every other field untouched.

    Field-at-a-time is the point: a long onboarding interview saves as it goes, so nothing is lost
    if it's abandoned. Identity fields are single values and are set outright (for the multi-part
    ones — ``contact``, ``headlines`` — pass the whole line). Sections **append** by default, so
    recording roles one at a time keeps every role — no ordinary write truncates a career history.
    ``mode="replace"`` rewrites a section: it's how a correction is made, by a caller that has just
    read the section in full and passes the whole fixed text.

    Refuses rather than guesses:

    * an unknown field → ``KeyError``; an unknown mode → ``ValueError``;
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
    # Load → edit → save under one lock. Parallel tool calls make this a genuine race, not a
    # theoretical one: without it, concurrent field writes silently drop each other's edits.
    with _locked():
        path = _path()
        raw, err = _read_json(path)
        if not err and raw is not None and not isinstance(raw, dict):
            err = "not a JSON object"
        if err:
            raise StoreUnreadable(path, err)
        prof = _normalize(raw) if raw else empty_profile()
        slot = "identity" if field in IDENTITY_FIELDS else "sections"
        old = prof[slot][field]
        new = text if slot == "identity" or mode == "replace" else _appended(old, text)
        if field in GUARDED_SECTIONS and not confirm_removal:
            dropped = _dropped(old, new)
            if dropped:
                raise _refuse_removal(field, dropped)
        if new == old:
            return {**prof, "change": "unchanged"}
        prof[slot][field] = new
        change = "set" if not old else ("appended" if slot == "sections" and mode == "append" else "replaced")
        return {**save_profile(prof), "change": change}


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
_TAG_OPENER = re.compile(r"<(?=\s*/?\s*[A-Za-z_!?])")


def _defang(text: str) -> str:
    """Neutralize tag-shaped text in a profile value, so nothing the agent recorded — say, a line
    harvested from an ingested CV — can close the ``<operator_profile>`` block (or the host frame
    around it) and speak as the block itself. Invisible format characters (zero-width space,
    joiners, direction overrides) are dropped first, and look-alike brackets folded to ASCII, so
    ``<`` + ZWSP + ``/operator_profile>`` or a fullwidth ``＜/operator_profile＞`` is caught too."""
    text = "".join(ch for ch in (text or "") if unicodedata.category(ch) != "Cf")
    return _TAG_OPENER.sub("&lt;", text.translate(_DELIMITER_LOOKALIKES))


def _one_line(text: str) -> str:
    """An identity value on one line: a scalar fact has no business adding lines to the block."""
    return " ".join(_defang(text).split())


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
            lines.append("")
            lines.append(f"{SECTIONS[k].upper()} — hard stops when writing in their name:")
            lines += [f"  {ln}" if ln.strip() else "" for ln in _defang(body).splitlines()]

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
# specific patterns come first ("Lines never to claim" is do_not_claim, not roles).
_HEADING_FIELDS: tuple[tuple[str, re.Pattern], ...] = (
    ("do_not_claim", re.compile(r"\b(do not|don't|never)\b.*\bclaim", re.I)),
    ("stories", re.compile(r"\bstor(y|ies)\b|\bstar\b", re.I)),
    ("education", re.compile(r"\beducation\b|\bcertific|\bcredential|\bdegree|\bqualification", re.I)),
    ("skills", re.compile(r"\bskill", re.I)),
    ("notes", re.compile(r"\bnotes?\b", re.I)),
    ("roles", re.compile(r"\broles?\b|\bexperience\b|\bemployment\b|\bwork history\b|\bcareer\b|\bpositions?\b", re.I)),
    ("identity", re.compile(r"\bidentity\b|\bcontact\b|\babout\b|\bpersonal\b", re.I)),
)
# A "- **Label:** value" / "Label: value" line in the identity section → the identity field.
_IDENTITY_LABELS: tuple[tuple[str, re.Pattern], ...] = (
    ("name", re.compile(r"\bname\b", re.I)),
    ("location", re.compile(r"\blocation\b|\bbased\b|\bcity\b", re.I)),
    ("work_auth", re.compile(r"\bauthori[sz]ation\b|\bvisa\b|\bright to work\b|\bwork auth", re.I)),
    ("contact", re.compile(r"\bcontact\b|\bemail\b|\bphone\b|\blinkedin\b|\bportfolio\b|\bgithub\b", re.I)),
    ("headlines", re.compile(r"\bheadline|\btitles?\b|\brole-type\b", re.I)),
)
_LABELLED = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*(?P<b>[^*]+?):?\*\*:?|(?P<p>[A-Za-z][^:*]{0,48}):)\s*(?P<value>.*)$")
_NOT_RECORDED = "_(not recorded)_"


def _labelled(line: str) -> tuple[str, str] | None:
    m = _LABELLED.match(line)
    if not m:
        return None
    return (m.group("b") or m.group("p") or "").strip(), m.group("value").strip()


def parse_experience(text: str, template_text: str = "") -> tuple[dict[str, str], list[str]]:
    """Read an operator-written ``Experience.md`` into profile fields: ``(fields, unmapped)``.

    Sections are found by their ``##`` heading (the template's, the export's, or a plain "Skills" /
    "Experience"), identity facts by ``Label: value`` lines. Every line that is still the shipped
    template's hint text is dropped — so a file where only the Name line was filled in yields just
    the name, never the template's example bullets — as are ``_(not recorded)_`` placeholders and
    the preamble (title and banner). Anything that doesn't map to a field comes back in ``unmapped``
    for the agent to raise, rather than being guessed into one."""
    template = {ln.strip() for ln in (template_text or "").splitlines() if ln.strip()}
    fields: dict[str, list[str]] = {}
    unmapped: list[str] = []
    heading, target = "", None  # None = preamble; "" = an unmapped section

    def keep(line: str) -> bool:
        s = line.strip()
        return bool(s) and s not in template and s != _NOT_RECORDED

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        h2 = re.match(r"^##\s+(.*?)\s*#*\s*$", line)
        if h2 and not line.startswith("###"):
            heading = h2.group(1)
            target = next((f for f, pat in _HEADING_FIELDS if pat.search(heading)), "")
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
            field = next((f for f, pat in _IDENTITY_LABELS if pair and pat.search(pair[0])), None)
            if field:
                fields.setdefault(field, []).append(value)
            else:
                unmapped.append(f"{heading}: {line.strip()}")
            continue
        if target == "skills":
            pair = _labelled(line)
            if pair and _HEADING_FIELDS[0][1].search(pair[0]):  # the template's "Explicitly do NOT claim:"
                if pair[1] and pair[1] != _NOT_RECORDED:
                    fields.setdefault("do_not_claim", []).append(pair[1])
                continue
        if target == "":
            unmapped.append(f"{heading}: {line.strip()}")
            continue
        fields.setdefault(target, []).append(line)

    out: dict[str, str] = {}
    for field, lines in fields.items():
        if field in IDENTITY_FIELDS:
            out[field] = " · ".join(dict.fromkeys(lines))
        else:
            body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
            if body:
                out[field] = body
    return out, unmapped


def import_fields(
    fields: dict[str, str], *, mode: str = "append", confirm_removal: bool = False, apply: bool = False
) -> list[dict]:
    """What importing ``fields`` would do to the profile — and, with ``apply=True``, do it — one
    row per field: ``{"field", "outcome", "detail"}``.

    Everything goes through ``update_field``, so the import obeys the agent's own rules: sections
    append (or replace, when asked), dropping a ``do_not_claim`` line needs ``confirm_removal``, an
    unreadable store raises ``StoreUnreadable``. Identity facts are filled in where the profile has
    none; one that differs from the profile is kept as-is unless ``mode="replace"``."""
    if mode not in UPDATE_MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of: {', '.join(UPDATE_MODES)}")
    path = _path()
    raw, err = _read_json(path)
    if err or (raw is not None and not isinstance(raw, dict)):
        raise StoreUnreadable(path, err or "not a JSON object")
    prof = _normalize(raw) if raw else empty_profile()
    rows: list[dict] = []
    for field in list(IDENTITY_FIELDS) + list(SECTIONS):
        text = (fields.get(field) or "").strip()
        if not text:
            continue
        if field in IDENTITY_FIELDS:
            old = prof["identity"][field]
            if old == text:
                rows.append({"field": field, "outcome": "unchanged", "detail": ""})
                continue
            if old and mode == "append":
                rows.append({"field": field, "outcome": "kept", "detail": f"file says {text!r}, profile keeps {old!r}"})
                continue
            outcome, detail = ("replaced" if old else "set"), repr(text)
        else:
            old = prof["sections"][field]
            new = text if mode == "replace" else _appended(old, text)
            if new == old:
                rows.append({"field": field, "outcome": "unchanged", "detail": ""})
                continue
            dropped = _dropped(old, new)
            if field in GUARDED_SECTIONS and dropped and not confirm_removal:
                rows.append(
                    {"field": field, "outcome": "refused", "detail": f"would remove {len(dropped)} hard stop(s)"}
                )
                continue
            had = set(_lines(old))
            added = len([ln for ln in _lines(new) if ln not in had])
            outcome = "set" if not old else ("replaced" if mode == "replace" else "appended")
            detail = f"{added} new line(s)" + (f", {len(dropped)} removed" if dropped else "")
        if apply:
            outcome = update_field(field, text, mode=mode, confirm_removal=confirm_removal)["change"]
        rows.append({"field": field, "outcome": outcome, "detail": detail})
    return rows
