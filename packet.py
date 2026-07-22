"""Role-packet workspace — the folder-per-role artifact tree, host-free.

The gated ``role-packet`` skill produces a *packet* of files per role, laid out the way a
real job hunt is filed:

    <root>/
      Companies/<Company>/Roles/<Role - Req>/
        job description (raw).md
        process_log.md
        recruiter brief.md
        hiring manager profile.md      (conditional)
        role evidence map.md
        tailored resume.md
        application skills entry list.md
        cover letter.md
        prompt transcript.md
        role packet.md                 (the assembled deliverable)
        orchestration log.md
      Resume/Experience.md             (per-candidate source of truth — from templates/)
      Agent/story-bank.md · Agent/experience-reviewer.md
      Skills/Humanize/SKILL.md · workflow-audit/improvements.md

Only *deterministic* mechanics live here — compute paths, scaffold the tree, write a
named artifact, assemble the packet, seed the workspace from templates. The *content* of
each artifact is the agent's job (driven by the skill). No host imports, so every function
is unit-testable with nothing but a temp dir — same contract as ``state.py`` / ``rubric.py``.

``CAREERCOACH_PACKET_DIR`` overrides the root (tests / power users); otherwise the resolved
``packet_root`` config wins, falling back to ``~/CareerCoach``.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

# The packet artifacts: slug (what tools accept + validate) → human filename (what's written).
# Ordering is the natural production order and drives the intake checklist.
ARTIFACTS: dict[str, str] = {
    "job-description-raw": "job description (raw).md",
    "process-log": "process_log.md",
    "recruiter-brief": "recruiter brief.md",
    "hiring-manager-profile": "hiring manager profile.md",
    "evidence-map": "role evidence map.md",
    "tailored-resume": "tailored resume.md",
    "skills-entry": "application skills entry list.md",
    "cover-letter": "cover letter.md",
    "prompt-transcript": "prompt transcript.md",
    "role-packet": "role packet.md",
    "orchestration-log": "orchestration log.md",
}

# Artifacts the agent authors at a gate (everything except the auto-managed log/packet files).
AUTHORED: tuple[str, ...] = (
    "recruiter-brief",
    "hiring-manager-profile",
    "evidence-map",
    "tailored-resume",
    "skills-entry",
    "cover-letter",
    "prompt-transcript",
)

# What goes into the assembled ``role packet.md`` body, in order (the shareable deliverable —
# the raw JD, logs, transcript and the packet itself are excluded to avoid noise/recursion).
PACKET_BODY: tuple[str, ...] = (
    "recruiter-brief",
    "hiring-manager-profile",
    "evidence-map",
    "tailored-resume",
    "skills-entry",
    "cover-letter",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def safe_name(text: str) -> str:
    """A filesystem-safe path segment that stays human-readable. Keeps spaces, commas and
    most punctuation (so ``Associate Director, Technical Product Management - R407969`` is
    preserved verbatim); replaces only path-hostile characters and collapses whitespace."""
    cleaned = re.sub(r'[/\\:*?"<>|\r\n\t]+', "-", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.strip(" .-")  # no leading/trailing dots/spaces/dashes (Windows-hostile)
    return cleaned or "untitled"


def role_dirname(role: str, req: str = "") -> str:
    """The role folder name: ``<Role> - <Req>`` when a requisition id is given, else ``<Role>``."""
    role = (role or "").strip()
    req = (req or "").strip()
    return safe_name(f"{role} - {req}" if req else role)


def resolve_root(configured: str = "") -> Path:
    """The workspace root. Precedence: ``CAREERCOACH_PACKET_DIR`` env (tests / override) →
    the resolved ``packet_root`` config → ``~/CareerCoach``. Created on demand."""
    env = os.environ.get("CAREERCOACH_PACKET_DIR", "").strip()
    raw = env or (configured or "").strip() or str(Path.home() / "CareerCoach")
    root = Path(raw).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def role_path(root, company: str, role: str, req: str = "") -> Path:
    """``<root>/Companies/<Company>/Roles/<Role - Req>`` — pure path math, creates nothing."""
    return Path(root) / "Companies" / safe_name(company) / "Roles" / role_dirname(role, req)


def artifact_path(root, company: str, role: str, req: str, artifact: str) -> Path:
    """Absolute path to one named artifact inside a role folder. Raises on an unknown slug."""
    if artifact not in ARTIFACTS:
        raise KeyError(f"unknown artifact {artifact!r}; known: {', '.join(ARTIFACTS)}")
    return role_path(root, company, role, req) / ARTIFACTS[artifact]


def append_log(root, company: str, role: str, req: str, line: str) -> Path:
    """Append a timestamped line to the role's ``process_log.md`` (created if missing)."""
    path = artifact_path(root, company, role, req, "process-log")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"# Process log — {role}\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"- {_now()} — {line}\n")
    return path


def scaffold_role(root, company: str, role: str, req: str = "", raw_jd: str = "") -> dict:
    """Create the role folder and seed the two intake files (raw JD + process log).

    Idempotent: an existing folder is reused and existing files are never clobbered — but a
    non-empty ``raw_jd`` fills the raw-JD file if it's still an empty placeholder. Returns a
    summary with the folder path and which files were created vs. already present."""
    folder = role_path(root, company, role, req)
    existed = folder.exists()
    folder.mkdir(parents=True, exist_ok=True)

    created: list[str] = []
    jd = folder / ARTIFACTS["job-description-raw"]
    placeholder = f"# Job description (raw) — {role}\n\n_Paste the full, unedited posting here._\n"
    if not jd.exists():
        jd.write_text((raw_jd.strip() + "\n") if raw_jd.strip() else placeholder, encoding="utf-8")
        created.append(ARTIFACTS["job-description-raw"])
    elif raw_jd.strip() and jd.read_text(encoding="utf-8").strip() == placeholder.strip():
        jd.write_text(raw_jd.strip() + "\n", encoding="utf-8")

    log = folder / ARTIFACTS["process-log"]
    if not log.exists():
        log.write_text(f"# Process log — {role} @ {company}\n\n", encoding="utf-8")
        created.append(ARTIFACTS["process-log"])
    append_log(root, company, role, req, "Intake — role folder scaffolded" + ("" if existed else " (new)"))

    return {"path": str(folder), "existed": existed, "created": created}


def write_artifact(root, company: str, role: str, req: str, artifact: str, content: str) -> dict:
    """Write one named packet artifact (validated slug → correct filename), creating the folder
    if needed, and log it to the process log. Returns the path and whether it replaced content."""
    path = artifact_path(root, company, role, req, artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    replaced = path.exists() and bool(path.read_text(encoding="utf-8").strip())
    path.write_text((content or "").rstrip() + "\n", encoding="utf-8")
    append_log(root, company, role, req, f"{'Updated' if replaced else 'Wrote'} {ARTIFACTS[artifact]}")
    return {"path": str(path), "artifact": artifact, "replaced": replaced}


def role_status(root, company: str, role: str, req: str = "") -> dict:
    """Which artifacts exist for a role and which are still missing (by slug)."""
    folder = role_path(root, company, role, req)
    present = [k for k, name in ARTIFACTS.items() if (folder / name).exists()]
    missing = [k for k in ARTIFACTS if k not in present]
    return {"path": str(folder), "present": present, "missing": missing}


def assemble_packet(root, company: str, role: str, req: str = "") -> dict:
    """Concatenate the present body artifacts into ``role packet.md`` and refresh the
    ``orchestration log.md`` completeness manifest. Returns present/missing body sections."""
    folder = role_path(root, company, role, req)
    if not folder.exists():
        raise FileNotFoundError(f"no role folder at {folder} — scaffold it first")

    present = [k for k in PACKET_BODY if (folder / ARTIFACTS[k]).exists()]
    missing = [k for k in PACKET_BODY if k not in present]

    parts = [f"# Role packet — {role} @ {company}", ""]
    if req.strip():
        parts.append(f"Requisition: {req.strip()}")
        parts.append("")
    parts.append(f"Assembled: {_now()}")
    parts.append("")
    for key in present:
        body = (folder / ARTIFACTS[key]).read_text(encoding="utf-8").strip()
        parts.append(f"\n---\n\n## {ARTIFACTS[key].removesuffix('.md').title()}\n")
        parts.append(body)
    if missing:
        parts.append("\n---\n\n## Not yet produced\n")
        parts.extend(f"- {ARTIFACTS[k]}" for k in missing)
    (folder / ARTIFACTS["role-packet"]).write_text("\n".join(parts).strip() + "\n", encoding="utf-8")

    status = role_status(root, company, role, req)
    orch = [
        f"# Orchestration log — {role} @ {company}",
        "",
        f"Assembled: {_now()}",
        "",
        "## Artifacts",
        *[f"- [{'x' if k in status['present'] else ' '}] {name}" for k, name in ARTIFACTS.items()],
    ]
    (folder / ARTIFACTS["orchestration-log"]).write_text("\n".join(orch) + "\n", encoding="utf-8")
    append_log(root, company, role, req, f"QA — assembled packet ({len(present)}/{len(PACKET_BODY)} sections)")

    return {"path": str(folder / ARTIFACTS["role-packet"]), "present": present, "missing": missing}


def list_roles(root) -> list[dict]:
    """Every role folder under ``<root>/Companies/*/Roles/*`` with a done/total artifact count."""
    base = Path(root) / "Companies"
    out: list[dict] = []
    if not base.exists():
        return out
    for company_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        roles_dir = company_dir / "Roles"
        if not roles_dir.exists():
            continue
        for rd in sorted(p for p in roles_dir.iterdir() if p.is_dir()):
            done = sum(1 for name in ARTIFACTS.values() if (rd / name).exists())
            out.append(
                {
                    "company": company_dir.name,
                    "role": rd.name,
                    "path": str(rd),
                    "artifacts": done,
                    "total": len(ARTIFACTS),
                }
            )
    return out


def init_workspace(root, templates_dir) -> dict:
    """Copy the plugin's ``templates/`` tree into the workspace root, never clobbering a file
    that already exists (protects the candidate's edits). Returns created/skipped relative paths."""
    templates_dir = Path(templates_dir)
    created: list[str] = []
    skipped: list[str] = []
    if not templates_dir.exists():
        return {"created": created, "skipped": skipped, "root": str(root)}
    for src in sorted(p for p in templates_dir.rglob("*") if p.is_file()):
        rel = src.relative_to(templates_dir)
        dest = Path(root) / rel
        if dest.exists():
            skipped.append(str(rel))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        created.append(str(rel))
    return {"created": created, "skipped": skipped, "root": str(root)}
