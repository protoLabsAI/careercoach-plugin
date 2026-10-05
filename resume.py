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

**Anti-fabrication.** This reformats verified profile content and adds **no** claims. It uses
only the identity scalars ``name`` / ``location`` / ``contact`` / ``headlines`` and the sections
``roles`` / ``education`` / ``skills``. It never emits ``do_not_claim``, ``stories`` or ``notes``
— guardrails and private tailoring notes are not résumé content. Every value is HTML-escaped, so
text harvested into a profile can't inject markup.

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

# The profile pieces a résumé is built from, in the order they appear in the document — and ONLY
# these. do_not_claim / stories / notes are deliberately absent: they are never résumé content.
IDENTITY_USED: tuple[str, ...] = ("name", "location", "contact", "headlines")
SECTIONS_USED: tuple[tuple[str, str], ...] = (
    ("roles", "Experience"),
    ("skills", "Skills"),
    ("education", "Education"),
)

# The required fields a résumé cannot be built without — an empty one of these is reported, not
# guessed at. (Location and headlines are nice-to-have; a name, a way to reach them, and at least
# one role are not.)
REQUIRED_FIELDS: tuple[str, ...] = ("name", "contact", "roles")

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


def _inline(text: str) -> str:
    """One line of profile text as safe inline HTML: escaped first (so ``<script>`` can't survive
    as markup), then the one inline mark the profile actually uses — ``**bold**`` — applied to the
    already-escaped text (which contains no raw ``<`` of its own)."""
    return _BOLD.sub(r"<strong>\1</strong>", html.escape(text, quote=False))


def _render_markdown(md: str) -> str:
    """A profile section's markdown → simple, escaped HTML: ATX headings (``#``..), ``-``/``*``/``+``
    bullet lists, and paragraphs. Headings start at ``<h3>`` (the section itself is the ``<h2>``) and
    clamp at ``<h4>``. Anything else is treated as paragraph text — a profile holds career prose,
    not a document, so there is no table/code/image handling to get wrong."""
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
            level = min(2 + len(h.group(1)), 4)  # # -> h3, ## -> h4, ### and deeper -> h4
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


def missing_for_resume(prof: dict) -> list[str]:
    """The REQUIRED_FIELDS (``name``, ``contact``, ``roles``) that are empty in ``prof`` — what a
    résumé cannot be built without. Empty list means it's buildable."""
    ident = prof.get("identity") or {}
    sect = prof.get("sections") or {}
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        source = ident if field in IDENTITY_USED else sect
        if not str(source.get(field) or "").strip():
            missing.append(field)
    return missing


def render_resume_html(prof: dict) -> str:
    """Build a self-contained résumé HTML document from the verified profile.

    Uses ONLY the identity scalars ``name`` / ``location`` / ``contact`` / ``headlines`` and the
    sections ``roles`` / ``education`` / ``skills``; never ``do_not_claim`` / ``stories`` /
    ``notes``. Section markdown becomes simple HTML and every value is HTML-escaped. Reformats
    verified content and adds no claims. Laid out for US Letter (what ``browser_pdf`` prints), with
    inline CSS and no external resources."""
    ident = prof.get("identity") or {}
    sect = prof.get("sections") or {}

    name = str(ident.get("name") or "").strip()
    headlines = str(ident.get("headlines") or "").strip()
    location = str(ident.get("location") or "").strip()
    contact = str(ident.get("contact") or "").strip()

    header: list[str] = []
    if name:
        header.append(f"<h1>{_inline(name)}</h1>")
    if headlines:
        header.append(f'<p class="headline">{_inline(headlines)}</p>')
    contact_line = " | ".join(part for part in (location, contact) if part)
    if contact_line:
        header.append(f'<p class="contact">{_inline(contact_line)}</p>')

    blocks: list[str] = ["\n".join(header)] if header else []
    for key, label in SECTIONS_USED:
        body = str(sect.get(key) or "").strip()
        if body:
            blocks.append(f"<section>\n<h2>{html.escape(label)}</h2>\n{_render_markdown(body)}\n</section>")

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
