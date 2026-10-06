"""Résumé HTML from the verified operator profile — host-free.

The one job of this module: turn the profile ``profile.load_profile()`` returns into a single,
self-contained HTML file that prints to a real PDF, so an ATS form that wants a résumé *file*
can get one. The pipeline it feeds (protoLabsAI/protoAgent#4032 phase 2):

    careercoach writes Resume/resume.html
      → the agent opens it with ``browser_open file://…``
      → ``browser_pdf("resume-<company>.pdf")`` prints the page into the browser plugin's own
        capture directory (the only place its ``browser_upload`` will read a file from)
      → the returned path goes to ``browser_upload``.

``browser_pdf`` always prints **US Letter** and ignores ``@page size``, so the document is laid
out for Letter; the ``@page`` rule is kept anyway for a download-and-print by hand. The HTML is
**entirely self-contained** — inline CSS, no external fonts, scripts, stylesheets or images — so
it renders the same as a ``file://`` page as it would anywhere; a print agent that fetched
nothing can't be surprised by a resource that didn't load.

**Anti-fabrication, and the privacy leak that reshaped this.** The résumé is built from ONLY the
identity scalar ``name`` and the single section ``sections.resume`` — the exact, operator-approved
résumé body. It does **not** read ``identity.location`` / ``identity.contact`` /
``identity.headlines`` or the sections ``roles`` / ``skills`` / ``education`` / ``do_not_claim`` /
``stories`` / ``notes`` at all. Those are the coach's *working record*: they carry internal
annotations ("do not re-litigate", "TENURE NOTE … UNCONFIRMED by Josh", "★ … verified in-browser",
"INHERITED stack … Do not let him claim otherwise") *inside* the very fields a résumé would want,
so excluding only ``do_not_claim`` / ``stories`` / ``notes`` was not enough — the 2026-10-05 GitLab
PDF printed those annotations verbatim from ``location`` / ``roles`` / ``skills`` / ``education``.
The fix is to stop reading the working record entirely and render a clean, separate source the
operator has approved. As defense in depth, ``annotation_lines`` scans that source for coach-note
markers and the renderer's caller REFUSES rather than print (or silently strip) a body that still
carries them. Every value is still HTML-escaped, so text in a profile can't inject markup.

Same contract as ``profile.py`` / ``packet.py``: no host (``graph.*``) imports, so every function
is unit-testable with nothing but a temp dir, and this file loads on its own, outside its package.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
from pathlib import Path

# Where the résumé file lands, under the packet workspace root.
RESUME_RELPATH = "Resume/resume.html"

# The profile pieces a résumé is built from — and ONLY these. The identity scalar ``name`` and the
# single section ``resume`` (the operator-approved body). Nothing else in the profile is read: the
# working-record fields (location / contact / headlines / roles / skills / education / do_not_claim
# / stories / notes) carry internal coach annotations and must never reach a résumé — see the
# module docstring and the 2026-10-05 leak.
IDENTITY_USED: tuple[str, ...] = ("name",)
SECTIONS_USED: tuple[tuple[str, str], ...] = (("resume", "Résumé body"),)

# The required fields a résumé cannot be built without — an empty one of these is reported, not
# guessed at: a name, and the operator-approved résumé body itself.
REQUIRED_FIELDS: tuple[str, ...] = ("name", "resume")

# Coach-note markers (case-insensitive). The résumé body must hold only employer-facing text; if any
# line of ``name`` or the ``resume`` body contains one of these, the tool REFUSES and writes nothing
# rather than print — or silently strip — an internal annotation. Modelled on the 2026-10-05 leak.
ANNOTATION_MARKERS: tuple[str, ...] = (
    "do not",
    "don't let",
    "date resolved",
    "tenure",
    "★",
    "do not claim",
    "not evidenced",
    "positioning:",
    "confirmed by",
    "unconfirmed",
    "ruling",
    "inherited stack",
    "hard constraint",
    "re-litigate",
    "on request",
    "evidence, not",
    "verified in-browser",
    "note:",
    "by josh",
)

# Inline, self-contained, ATS-safe CSS — the same contract the shipped resume templates hold:
# single column, standard headings, no ligatures, a real generic font fallback, and entries that
# don't split across a page. US Letter, because browser_pdf prints Letter whatever @page says.
# No url(), no @import, no external font — nothing the browser has to fetch.
_CSS = """\
  @page { size: Letter; margin: 0.6in; }

  *, *::before, *::after { box-sizing: border-box; }

  html { background: #ffffff; }

  body {
    font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif;
    font-variant-ligatures: none;   /* fi/fl ligatures reach a text parser as U+FB0x */
    font-kerning: none;
    font-size: 10.5pt;
    font-weight: 400;
    line-height: 1.38;
    color: #111111;
    background: #ffffff;
    max-width: 7.3in;               /* 8.5in page - 2 x 0.6in margin */
    margin: 0 auto;
    padding: 0.6in 0;               /* mimics the print margin on screen */
    -webkit-text-size-adjust: 100%;
  }

  @media print {
    body { max-width: none; margin: 0; padding: 0; }
  }

  h1 {
    font-size: 19pt;
    font-weight: 700;
    line-height: 1.15;
    letter-spacing: 0.01em;
    margin: 0 0 2pt;
    color: #111111;
  }

  .headline { font-size: 11pt; font-weight: 400; color: #444444; margin: 0 0 4pt; }
  /* Contact is plain visible text (many parsers ignore href) separated by a literal " | ". */
  .contact { font-size: 9.5pt; font-weight: 400; color: #444444; margin: 0 0 14pt; }

  h2 {
    font-size: 11pt;
    font-weight: 700;
    line-height: 1.2;
    color: #111111;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin: 16pt 0 6pt;
    padding-bottom: 2pt;
    border-bottom: 0.75pt solid #999999;
    break-after: avoid;
    page-break-after: avoid;
  }

  h3 {
    font-size: var(--body-size, 10.5pt);
    font-weight: 700;
    line-height: 1.25;
    margin: 10pt 0 2pt;
    break-after: avoid;
    page-break-after: avoid;
    break-inside: avoid;
    page-break-inside: avoid;
  }

  h4 {
    font-size: 10.5pt;
    font-weight: 700;
    line-height: 1.25;
    margin: 6pt 0 2pt;
    break-after: avoid;
    page-break-after: avoid;
  }

  p { margin: 0 0 6pt; }
  strong { font-weight: 700; }

  ul { list-style: disc; margin: 3pt 0 6pt; padding-left: 16pt; }
  li { margin: 0 0 3pt; break-inside: avoid; page-break-inside: avoid; }

  p, li { orphans: 2; widows: 2; }\
"""

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")

# A level-2 heading opens a new résumé section (``## Experience``); ``## `` requires whitespace
# after the two hashes, so a ``### `` role heading is NOT a section boundary — it renders inside the
# section as an ``<h3>``. The trailing ``#``/space are trimmed off the label.
_SECTION = re.compile(r"^##\s+(.*)$")
# An optional header line at the very top of the body: ``Headline: …`` / ``Location: …`` /
# ``Contact: …`` (case-insensitive key). Location and Contact form the contact line; Headline is the
# ``.headline`` paragraph; everything else before the first ``## `` is a summary paragraph.
_HEADER_LINE = re.compile(r"^(headline|location|contact)\s*:\s*(.*)$", re.I)


def _inline(text: str) -> str:
    """One line of profile text as safe inline HTML: escaped first (so ``<script>`` can't survive
    as markup), then the one inline mark the profile actually uses — ``**bold**`` — applied to the
    already-escaped text (which contains no raw ``<`` of its own)."""
    return _BOLD.sub(r"<strong>\1</strong>", html.escape(text, quote=False))


def _render_markdown(md: str) -> str:
    """One résumé section's inner markdown → simple, escaped HTML: ATX headings, ``-``/``*``/``+``
    bullet lists, and paragraphs. The section's own ``## `` heading is rendered by the caller as the
    ``<h2>``; what reaches here is the body under it, so a ``### `` role heading becomes an ``<h3>``
    and ``####`` an ``<h4>`` (floored at ``<h3>`` — nothing in a section body becomes ``<h1>``/``<h2>``
    and competes with the name or the section heading). Anything else is paragraph text — a résumé
    body holds career prose, not a document, so there is no table/code/image handling to get wrong."""
    out: list[str] = []
    bullets: list[str] = []
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            out.append("<ul>")
            out.extend(f"<li>{_inline(b)}</li>" for b in bullets)
            out.append("</ul>")
            bullets.clear()

    for raw in (md or "").splitlines():
        line = raw.strip()
        if not line:
            flush_para()
            flush_bullets()
            continue
        h = _HEADING.match(line)
        if h:
            flush_para()
            flush_bullets()
            level = min(max(len(h.group(1)), 3), 4)  # ### -> h3, #### and deeper -> h4, floored at h3
            out.append(f"<h{level}>{_inline(h.group(2).strip())}</h{level}>")
            continue
        b = _BULLET.match(line)
        if b:
            flush_para()
            bullets.append(b.group(1).strip())
            continue
        flush_bullets()
        para.append(line)
    flush_para()
    flush_bullets()
    return "\n".join(out)


def _split_resume_body(body: str) -> tuple[list[str], str]:
    """``(pre_heading_lines, section_markdown)``: everything before the first ``## `` heading, and
    the section portion from that heading on. A body with no ``## `` heading is all pre-heading."""
    lines = (body or "").splitlines()
    for i, raw in enumerate(lines):
        if _SECTION.match(raw.strip()):
            return lines[:i], "\n".join(lines[i:])
    return lines, ""


def _paragraphs(lines: list[str]) -> list[str]:
    """Blank-line-separated runs of text joined into one paragraph each (leading markers trimmed)."""
    paras: list[str] = []
    cur: list[str] = []
    for raw in lines:
        s = raw.strip()
        if s:
            cur.append(s)
        elif cur:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


def _parse_header(pre_lines: list[str]) -> tuple[str, str, str, list[str]]:
    """The optional header of a résumé body → ``(headline, location, contact, summary_paragraphs)``.
    ``Headline:`` / ``Location:`` / ``Contact:`` lines (case-insensitive, first wins) are pulled out;
    anything else before the first ``## `` section is summary prose."""
    headline = location = contact = ""
    rest: list[str] = []
    for raw in pre_lines:
        m = _HEADER_LINE.match(raw.strip()) if raw.strip() else None
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if val:
                if key == "headline" and not headline:
                    headline = val
                elif key == "location" and not location:
                    location = val
                elif key == "contact" and not contact:
                    contact = val
            continue
        rest.append(raw)
    return headline, location, contact, _paragraphs(rest)


def _render_sections(rest: str) -> list[str]:
    """The ``## ``-delimited section portion of a résumé body → ``<section><h2>…</h2>…</section>``
    blocks, each heading's body rendered with ``_render_markdown`` (so ``### `` becomes an ``<h3>``)."""
    sections: list[tuple[str, list[str]]] = []
    for raw in (rest or "").splitlines():
        m = _SECTION.match(raw.strip())
        if m:
            sections.append((m.group(1).strip().rstrip("#").strip(), []))
        elif sections:
            sections[-1][1].append(raw)
    out: list[str] = []
    for label, body_lines in sections:
        inner = _render_markdown("\n".join(body_lines))
        body = f"\n{inner}" if inner else ""
        out.append(f"<section>\n<h2>{_inline(label)}</h2>{body}\n</section>")
    return out


def annotation_lines(prof: dict) -> list[str]:
    """Lines of ``name`` or the ``resume`` body that carry a coach-note marker (``ANNOTATION_MARKERS``,
    case-insensitive) — defense in depth against the 2026-10-05 leak. Each offending line is returned
    trimmed to ~120 characters; an empty list means the body is clean. The résumé tool quotes these
    and refuses rather than print (or silently strip) an internal annotation."""
    ident = prof.get("identity") or {}
    sect = prof.get("sections") or {}
    hits: list[str] = []
    for text in (str(ident.get("name") or ""), str(sect.get("resume") or "")):
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            low = line.lower()
            if any(marker in low for marker in ANNOTATION_MARKERS):
                hits.append(line if len(line) <= 120 else line[:119] + "…")
    return hits


def missing_for_resume(prof: dict) -> list[str]:
    """What a résumé cannot be built without, empty when it's buildable:

    * ``name`` and/or ``resume`` — the REQUIRED_FIELDS that are empty (no name, or no approved body);
    * ``contact`` — when the body HAS ``## `` sections but no ``Contact:`` header line, so the
      résumé would carry no way to reach the operator (refused, as the old renderer refused a missing
      contact). Reported as the code ``contact``; the tool names it "résumé Contact line"."""
    ident = prof.get("identity") or {}
    sect = prof.get("sections") or {}
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        source = ident if field in IDENTITY_USED else sect
        if not str(source.get(field) or "").strip():
            missing.append(field)
    if "resume" not in missing:
        body = str(sect.get("resume") or "").strip()
        pre, rest = _split_resume_body(body)
        _, _, contact, _ = _parse_header(pre)
        if rest.strip() and not contact:
            missing.append("contact")
    return missing


def render_resume_html(prof: dict) -> str:
    """Build a self-contained résumé HTML document from the operator-approved résumé body.

    Reads ONLY ``identity.name`` and ``sections.resume`` — never the coach's working record
    (``location`` / ``contact`` / ``headlines`` / ``roles`` / ``skills`` / ``education`` /
    ``do_not_claim`` / ``stories`` / ``notes``), whose fields carry internal annotations (the
    2026-10-05 leak). The résumé body: optional ``Headline:`` / ``Location:`` / ``Contact:`` header
    lines and summary prose before the first ``## `` heading, then ``## `` sections (``### `` role
    headings and ``-`` bullets inside). ``Location`` and ``Contact`` form the contact line joined by
    " | ". Every value is HTML-escaped. Laid out for US Letter (what ``browser_pdf`` prints), with
    inline CSS and no external resources. Callers check ``missing_for_resume`` and ``annotation_lines``
    first — this reformats and adds no claims, but it does not itself police the body's content."""
    ident = prof.get("identity") or {}
    sect = prof.get("sections") or {}

    name = str(ident.get("name") or "").strip()
    body = str(sect.get("resume") or "").strip()

    pre, rest = _split_resume_body(body)
    headline, location, contact, summary = _parse_header(pre)

    header: list[str] = []
    if name:
        header.append(f"<h1>{_inline(name)}</h1>")
    if headline:
        header.append(f'<p class="headline">{_inline(headline)}</p>')
    contact_line = " | ".join(part for part in (location, contact) if part)
    if contact_line:
        header.append(f'<p class="contact">{_inline(contact_line)}</p>')
    for para in summary:
        header.append(f"<p>{_inline(para)}</p>")

    blocks: list[str] = ["\n".join(header)] if header else []
    blocks.extend(_render_sections(rest))

    title = f"{name} — Resume" if name else "Resume"
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title, quote=False)}</title>\n"
        f"<style>\n{_CSS}\n</style>\n"
        "</head>\n"
        "<body>\n" + "\n\n".join(blocks) + "\n</body>\n</html>\n"
    )


def _slug(text: str) -> str:
    """A filename-safe lowercase-hyphen slug, or ``""`` for blank input."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")


def pdf_name(company: str = "") -> str:
    """The PDF filename to pass to ``browser_pdf``: ``resume-<company-slug>.pdf``, or ``resume.pdf``
    with no company. A bare filename on purpose — ``browser_pdf`` writes into the browser plugin's
    own capture folder and refuses an absolute path."""
    slug = _slug(company)
    return f"resume-{slug}.pdf" if slug else "resume.pdf"


def _atomic_write(path: Path, text: str) -> None:
    """Write via temp file + rename, so the file is never observed half-written."""
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


def resume_path(root) -> Path:
    """Absolute path to the résumé file under the workspace root (``<root>/Resume/resume.html``)."""
    return Path(root) / RESUME_RELPATH


def write_resume_html(root, prof: dict) -> Path:
    """Render the profile to résumé HTML and write it atomically to ``<root>/Resume/resume.html``.
    Returns the path. Callers check ``missing_for_resume`` first and don't call this when it's
    non-empty — a résumé is never written from an incomplete profile."""
    path = resume_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, render_resume_html(prof))
    return path
