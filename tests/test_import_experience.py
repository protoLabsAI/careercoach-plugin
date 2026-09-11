"""``careercoach_import_experience``: the one way a workspace ``Resume/Experience.md`` reaches the
profile. Preview then apply, bound to what the operator saw; template hint text dropped; nothing
moved between sections; the profile's own write rules (line merge, size caps, hard stops) applied.

The cases here began as the third review round's reproducers.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import random
import re
import shutil
import time
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

V1 = """# Experience

## Identity
- **Name:** Ada Lovelace
- **Location:** London

## Roles (most recent first)

### Analyst — Engine Co · 1842–1843
- Wrote Note G, the first published algorithm
- Translated Menabrea's paper, tripling its length with notes

## Skills inventory (be honest about level)
- **Expert / can lead on:** symbolic computation

## Lines never to claim on their behalf
- Never imply she built the Engine
- No formal university degree
"""


def _experience(iso) -> Path:
    path = iso / "ws" / "Resume" / "Experience.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _preview_id(text: str) -> str:
    found = re.search(r"preview_id='([0-9a-f]+)'", text)
    return found.group(1) if found else ""


def _import(tools, **kw) -> tuple[str, str]:
    """Preview, then apply exactly that preview. Returns ``(preview, applied)``."""
    preview = tools["careercoach_import_experience"].invoke(dict(kw))
    pid = _preview_id(preview)
    if not pid:
        return preview, ""
    return preview, tools["careercoach_import_experience"].invoke({**kw, "apply": True, "preview_id": pid})


def _dupes(text: str) -> dict:
    counts = Counter(ln.strip() for ln in text.splitlines() if ln.strip())
    return {ln: n for ln, n in counts.items() if n > 1}


# ── re-import after an edit: only the new lines, where they belong ────────────────────
def test_reimporting_after_an_edit_adds_only_the_new_lines(tools, iso):
    """The template says "re-import after you edit it". Adding one bullet and one hard stop must
    add exactly those two lines — the whole section used to be appended again."""
    exp = _experience(iso)
    exp.write_text(V1, encoding="utf-8")
    _import(tools)
    exp.write_text(
        V1.replace(
            "tripling its length with notes\n",
            "tripling its length with notes\n- Corresponded with Faraday\n",
        ).replace("No formal university degree\n", "No formal university degree\n- Never claim fluency in German\n"),
        encoding="utf-8",
    )

    preview, applied = _import(tools)

    roles = tools["careercoach_get_profile"].invoke({"field": "roles"})
    dnc = tools["careercoach_get_profile"].invoke({"field": "do_not_claim"})
    assert not _dupes(roles) and not _dupes(dnc), f"{preview}\n{_dupes(roles)} {_dupes(dnc)}"
    assert "roles: appended — 1 new line(s)" in preview and "+ - Corresponded with Faraday" in preview
    assert "do_not_claim: appended — 1 new line(s)" in preview
    # The new bullet sits under its own role, not at the end of the section.
    assert "- Translated Menabrea's paper, tripling its length with notes\n- Corresponded with Faraday" in roles
    assert (
        preview.replace("Import preview — nothing written yet", "Imported").split("Show the operator")[0]
        == applied.split("Profile now")[0]
    ), "the preview promised something else than the apply wrote"


def test_reimporting_an_unedited_file_is_a_no_op(tools, iso):
    _experience(iso).write_text(V1, encoding="utf-8")
    _import(tools)
    preview, _ = _import(tools)
    assert "Nothing new to import" in preview


def test_import_with_nothing_to_import(tools, iso):
    assert "nothing to import" in tools["careercoach_import_experience"].invoke({})
    tools["careercoach_init_workspace"].invoke({})
    assert "untouched template" in tools["careercoach_import_experience"].invoke({})


# ── export → import is a round trip, in both modes ───────────────────────────────────
TRICKY = {
    "identity": {
        "name": "Grace Hopper",
        "location": "Arlington, VA",
        "work_auth": "US citizen",
        "contact": "grace@navy.example · linkedin.com/in/grace",
        "headlines": "Rear Admiral · Compiler lead",
    },
    "sections": {
        # a "do NOT claim" labelled line inside skills, and an H2 inside stories: the shapes the
        # live jobCoach profile has, which a replace-mode re-import used to mangle
        "skills": "- **Expert:** COBOL, compilers\n- **Explicitly do NOT claim:** Kubernetes at scale",
        "stories": "### Migration story\n- S: legacy billing\n\n## Project Atlas\n- S: 40 services\n- R: 99.95% uptime",
        "roles": "### Rear Admiral — US Navy · 1983–1986\n- Navy-wide data automation\n\n### Staff, Harvard\n- Mark I",
        "education": "- **PhD, Mathematics** — Yale, 1934.",
        "do_not_claim": "- No hardware design\n- Never imply she wrote UNIVAC's OS alone",
        "notes": "- Roles you're targeting; industries to lean into / avoid.",  # a template hint line, as content
    },
}


def _profiles():
    yield "tricky", TRICKY
    rng = random.Random(1842)
    pool = [
        "- a bullet with **bold** and a colon: like this",
        "### A Role — Company · 2019–2024",
        "## An H2 inside a section",
        "_(a parenthetical)_",
        "- 10⁶ users, ≥99.9% uptime",
        "",
        "- **Scope:** team size, budget, remit, who you reported to.",  # template hint text
    ]
    for i in range(6):
        sections = {
            k: "\n".join(rng.sample(pool, rng.randint(1, len(pool)))).strip()
            for k in ("roles", "education", "skills", "do_not_claim", "stories", "notes")
        }
        yield f"random-{i}", {"identity": {"name": f"Person {i}", "location": "Somewhere"}, "sections": sections}


@pytest.mark.parametrize("mode", ["append", "replace"])
@pytest.mark.parametrize("name,seed", list(_profiles()), ids=[n for n, _ in _profiles()])
def test_importing_the_export_back_changes_nothing(tools, profile, iso, mode, name, seed):
    """Whatever the profile holds, `export` then `import` must be a no-op in both modes: the export
    is the profile's own rendering, so nothing in it is new and nothing may be moved or dropped."""
    for field, value in {**seed["identity"], **seed["sections"]}.items():
        if value:
            profile.update_field(field, value, mode="replace", confirm_removal=True)
    before = profile.load_profile()
    tools["careercoach_export_experience"].invoke({})
    shutil.copyfile(_experience(iso).parent / "Experience (profile export).md", _experience(iso))

    preview, applied = _import(tools, mode=mode)

    after = profile.load_profile()
    changed = {
        k: (before[s][k], after[s][k])
        for s in ("identity", "sections")
        for k in before[s]
        if before[s][k] != after[s][k]
    }
    assert not changed, f"{name}/{mode} changed {list(changed)}:\n{preview}"
    assert "Nothing new to import" in preview and applied == ""


# ── nothing is moved between sections ────────────────────────────────────────────────
def test_an_unknown_heading_stays_in_its_section_and_is_reported(tools, profile, iso):
    _experience(iso).write_text(
        "## Skills inventory (be honest about level)\n- **Expert:** compilers\n\n"
        "## Publications\n- A Manual of Operation (1946)\n",
        encoding="utf-8",
    )
    preview, _ = _import(tools)
    assert "stays part of skills" in preview
    assert "A Manual of Operation" in profile.load_profile()["sections"]["skills"]


def test_a_do_not_claim_label_inside_skills_stays_in_skills(tools, profile, iso):
    """Moving it would rewrite the operator's skills and their hard stops behind their back — and
    it broke the export round trip. It's reported instead."""
    _experience(iso).write_text(
        "## Skills inventory (be honest about level)\n- **Explicitly do NOT claim:** Kubernetes at scale\n",
        encoding="utf-8",
    )
    preview, _ = _import(tools)
    prof = profile.load_profile()
    assert "Kubernetes" in prof["sections"]["skills"] and not prof["sections"]["do_not_claim"]
    assert "stays in skills" in preview


# ── the shipped template's own slots ─────────────────────────────────────────────────
def test_the_template_feeds_both_location_and_work_auth(tools, profile, iso):
    """0.7 splits the template's combined line, and an older seeded copy still has it: either way
    work_auth must not stay missing, or the agent asks for what the operator already wrote."""
    exp = _experience(iso)
    tools["careercoach_init_workspace"].invoke({})
    exp.write_text(
        exp.read_text()
        .replace("- **Name:**", "- **Name:** Ada Lovelace", 1)
        .replace("- **Location:**", "- **Location:** London", 1)
        .replace("- **Work authorization:**", "- **Work authorization:** UK citizen", 1),
        encoding="utf-8",
    )
    _import(tools)
    got = profile.load_profile()["identity"]
    assert got["location"] == "London" and got["work_auth"] == "UK citizen"


def test_an_older_seeded_template_still_fills_work_auth(tools, profile, iso):
    _experience(iso).write_text(
        "## Identity\n- **Name:** Ada Lovelace\n"
        "- **Location / work authorization:** London · UK citizen, no sponsorship\n",
        encoding="utf-8",
    )
    _import(tools)
    got = profile.load_profile()["identity"]
    assert got["location"] == "London" and got["work_auth"] == "UK citizen, no sponsorship"


def test_the_templates_hint_text_is_never_imported(tools, profile, iso):
    tools["careercoach_init_workspace"].invoke({})
    exp = _experience(iso)
    exp.write_text(exp.read_text().replace("- **Name:**", "- **Name:** Ada Lovelace", 1), encoding="utf-8")
    preview, _ = _import(tools)
    prof = profile.load_profile()
    assert prof["identity"]["name"] == "Ada Lovelace"
    assert profile.completeness(prof)["filled"] == 1, prof
    assert "grew ARR" not in preview and "team size, budget" not in preview


# ── preview → apply binding ──────────────────────────────────────────────────────────
def test_apply_needs_the_preview_the_operator_saw(tools, profile, iso):
    exp = _experience(iso)
    exp.write_text(V1, encoding="utf-8")
    bare = tools["careercoach_import_experience"].invoke({"apply": True})
    assert bare.startswith("Not imported") and "preview_id" in bare
    assert profile.completeness()["empty"]

    preview = tools["careercoach_import_experience"].invoke({})
    pid = _preview_id(preview)
    assert profile.completeness()["empty"], "a preview must not write"
    # The file changes after the operator saw the preview: the apply refuses.
    exp.write_text(
        V1.replace("- **Location:** London\n", "- **Location:** London\n- **Work authorization:** sponsored\n"),
        encoding="utf-8",
    )
    out = tools["careercoach_import_experience"].invoke({"apply": True, "preview_id": pid})
    assert out.startswith("Not imported") and "changed since that preview" in out
    assert profile.completeness()["empty"] and "work_auth" not in out
    # So does a different mode or confirm flag for the same preview_id.
    exp.write_text(V1, encoding="utf-8")
    assert "changed since that preview" in tools["careercoach_import_experience"].invoke(
        {"apply": True, "preview_id": pid, "mode": "replace"}
    )
    applied = tools["careercoach_import_experience"].invoke({"apply": True, "preview_id": pid})
    assert applied.startswith("Imported") and profile.load_profile()["identity"]["name"] == "Ada Lovelace"


def test_a_stale_preview_is_refused_after_the_profile_moves_on(tools, profile, iso):
    _experience(iso).write_text(V1, encoding="utf-8")
    pid = _preview_id(tools["careercoach_import_experience"].invoke({}))
    profile.update_field("roles", "### Something recorded in chat since")
    out = tools["careercoach_import_experience"].invoke({"apply": True, "preview_id": pid})
    assert out.startswith("Not imported") and "preview again" in out


def test_one_restore_undoes_a_whole_import(tools, profile, iso):
    """An import writes every field in one save, so the .bak it leaves is the pre-import profile."""
    profile.update_field("name", "Ada")
    _experience(iso).write_text(V1, encoding="utf-8")
    _import(tools)

    assert profile.completeness()["filled"] >= 4
    restored = json.loads(profile.backup_path(profile._path()).read_text())
    assert restored["identity"]["name"] == "Ada" and not restored["sections"]["roles"]


# ── size caps ────────────────────────────────────────────────────────────────────────
def test_one_import_cannot_fill_every_model_call_with_a_pasted_posting(tools, profile, iso):
    """do_not_claim is injected IN FULL on every call, and shrinking it needs confirm_removal — so
    the import must refuse an oversized one rather than park 80k tokens in every turn."""
    _experience(iso).write_text(
        "## Identity\n- **Name:** Ada\n\n## Things I would never claim\n"
        + "".join(f"- pasted posting line {i}: we value ownership and never settle\n" for i in range(5000)),
        encoding="utf-8",
    )
    preview, applied = _import(tools)

    assert "do_not_claim: too long" in preview and "the cap is 3,000" in preview
    assert not profile.load_profile()["sections"]["do_not_claim"]
    assert profile.load_profile()["identity"]["name"] == "Ada"  # the rest of the import still lands
    block = profile.context_block()
    assert len(block) < 10_000, f"always-on block is {len(block):,} chars"


def test_the_caps_bound_the_always_on_block(profile, iso):
    profile.update_field("do_not_claim", "- " + "x" * (profile._limit("do_not_claim") - 10))
    for field in ("name", "location", "work_auth", "contact", "headlines"):
        profile.update_field(field, "y" * (profile.IDENTITY_LIMIT - 1))
    for field in ("roles", "education", "skills", "stories", "notes"):
        profile.update_field(field, "z" * 10_000)
    block = profile.context_block()
    assert len(block) < 10_000, f"{len(block):,} chars with every field at its cap"
    identity_lines = [ln for ln in block.splitlines() if ln.startswith("  name:")]
    assert identity_lines and len(identity_lines[0]) <= profile._IDENTITY_SHOWN + 20  # clipped in the block


def test_a_value_stored_before_the_caps_is_clipped_in_the_block(profile, iso):
    path = profile._path()
    huge = "- never claim " + "q" * 50_000
    path.write_text(json.dumps({"identity": {"name": "Ada"}, "sections": {"do_not_claim": huge}}), encoding="utf-8")
    block = profile.context_block()
    assert len(block) < 10_000 and "more characters" in block and "careercoach_get_profile('do_not_claim')" in block


def test_a_write_past_a_cap_is_refused_with_the_reason(profile, tools, iso):
    out = tools["careercoach_update_profile"].invoke({"field": "do_not_claim", "content": "- " + "x" * 4000})
    assert "Refused" in out and "cap is 3,000" in out and "in full on every call" in out
    assert not profile.load_profile()["sections"]["do_not_claim"]
    with pytest.raises(ValueError):
        profile.update_field("roles", "x" * 25_000)


# ── the parser stays linear on pathological input ────────────────────────────────────
def _parse_in_child(src: str, text: str) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("cc_parse_child", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.parse_experience(text)
    mod.context_block({"identity": {"name": text[:400]}, "sections": {"do_not_claim": text[:400]}, "updated": ""})


PATHOLOGICAL = (
    "## Skills" + " " * 20_000 + "Proficiency\n- x\n",  # the heading regex: cubic backtracking
    "## " + "never " * 4_000 + "\n- x\n",  # the do_not_claim heading pattern
    "## Identity\n- **" + "a" * 20_000 + "\n",  # the labelled-line pattern
    "## Identity\n- **Name:** <" + " " * 20_000 + "1\n",  # the tag-opener lookahead
)


@pytest.mark.parametrize("text", PATHOLOGICAL, ids=["heading-spaces", "heading-never", "label", "tag-opener"])
def test_parsing_is_linear_on_pathological_input(profile, text):
    """Each of these took seconds to minutes before; the import tool and (on an empty profile)
    careercoach_read_profile both reach the parser, so they were a hang on operator-supplied text."""
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_parse_in_child, args=(profile.__file__, text))
    started = time.monotonic()
    proc.start()
    proc.join(20)
    alive = proc.is_alive()
    if alive:
        proc.kill()
    assert not alive, f"still parsing after {time.monotonic() - started:.0f}s"
    assert proc.exitcode == 0
