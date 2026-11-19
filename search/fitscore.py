"""The quick fit score every new role gets before it reaches the inbox — pure, host-free, explainable.

The watch's old score was the title match it had already filtered on, so every role that passed
scored 100 and the threshold filtered nothing. This scores what the filter can't see, out of the
posting's own text and the operator's profile:

- **stack** (50) — of the known technologies the posting names, the share the profile also names.
  A compiler or inference-kernel role names C++/LLVM/CUDA, which the profile doesn't, and sinks.
- **seniority** (20) — read off the title: intern/junior low, Staff/Lead lower, Principal and up lowest.
- **title** (15) — the share of the best-matching target role's words in the title (the old score).
- **pay** (15) — the posted range against ``min_salary`` (annual). Off when no floor is set;
  an unposted range is neutral, never a penalty.

A posting with under ``THIN_CHARS`` of text (an excerpt) is capped at ``THIN_CAP``: there isn't enough
of it to justify a high score. Pay in another currency isn't compared to the (USD) floor.

It is a triage proxy, not the LLM rubric (``record/rubric.py``); the coach still evaluates a role
before recommending it. A posting with too little text to judge (under three known technologies)
scores the stack part neutral, and the reason says so.
"""

from __future__ import annotations

import re

WEIGHTS = {"stack": 50, "seniority": 20, "title": 15, "pay": 15}
NEUTRAL = 60  # an unknown part: neither a reason to surface a role nor to bury it
MIN_TERMS = 3  # fewer known technologies than this in a posting = too thin to judge its stack
THIN_CHARS = 600  # less posting text than this (an excerpt, not the posting) caps the score
THIN_CAP = 70

# canonical name → regex alternatives (matched on word boundaries, case-insensitive)
TECH: dict[str, str] = {
    "python": r"python",
    "typescript": r"typescript|\bts\b",
    "javascript": r"javascript|\bjs\b|ecmascript",
    "react": r"react(?:\.js|js)?(?!\s*native)",
    "react native": r"react\s*native",
    "next.js": r"next\.?js",
    "vue": r"vue(?:\.js)?",
    "angular": r"angular",
    "svelte": r"svelte",
    "node": r"node(?:\.js|js)?",
    "deno": r"deno",
    "html": r"html5?",
    "css": r"css3?|tailwind|sass|scss",
    "web components": r"web\s*components|custom\s*elements|lit(?:-element)?",
    "storybook": r"storybook",
    "design systems": r"design\s*systems?|component\s*librar(?:y|ies)",
    "figma": r"figma",
    "accessibility": r"accessibility|a11y|wcag",
    "graphql": r"graphql",
    "rest": r"rest(?:ful)?\s*apis?",
    "fastapi": r"fastapi",
    "django": r"django",
    "flask": r"flask",
    "rails": r"rails|ruby\s+on\s+rails",
    "ruby": r"ruby",
    "go": r"golang|\bgo\b(?=\s*(?:,|/|and|or|\)|services|programming|language))",
    "rust": r"rust",
    "java": r"java(?!script)",
    "kotlin": r"kotlin",
    "scala": r"scala",
    "c#": r"c#|\.net|dotnet",
    "c++": r"c\+\+|cpp",
    "swift": r"swift",
    "php": r"php|laravel",
    "elixir": r"elixir|phoenix",
    "sql": r"sql",
    "postgres": r"postgres(?:ql)?",
    "mysql": r"mysql",
    "sqlite": r"sqlite",
    "mongodb": r"mongo(?:db)?",
    "redis": r"redis",
    "kafka": r"kafka",
    "elasticsearch": r"elasticsearch|opensearch",
    "vector databases": r"vector\s*(?:db|database|store)s?|pgvector|pinecone|weaviate|qdrant|chroma",
    "docker": r"docker|containers?",
    "kubernetes": r"kubernetes|k8s|helm",
    "terraform": r"terraform|pulumi",
    "aws": r"aws|amazon\s+web\s+services|lambda|bedrock",
    "gcp": r"gcp|google\s+cloud|vertex",
    "azure": r"azure",
    "ci/cd": r"ci/cd|github\s+actions|ci\s+pipelines?|continuous\s+integration",
    "git": r"git(?!hub\s+copilot)",
    "linux": r"linux|unix",
    "llm": r"llms?|large\s+language\s+models?|gpt|claude|gemini|foundation\s+models?",
    "agents": r"agents?|agentic|multi-agent",
    "rag": r"rag|retrieval[-\s]augmented",
    "prompt engineering": r"prompt\s*engineering|prompting|prompts?",
    "evals": r"evals?|evaluations?\s+(?:framework|harness|pipelines?)|llm[-\s]as[-\s]a[-\s]judge",
    "langchain": r"langchain|langgraph|llamaindex|crewai|autogen",
    "openai api": r"openai|anthropic",
    "mcp": r"mcp|model\s+context\s+protocol",
    "embeddings": r"embeddings?",
    "fine-tuning": r"fine[-\s]?tun(?:e|ing)|lora|rlhf",
    "pytorch": r"pytorch|torch",
    "tensorflow": r"tensorflow|keras|jax",
    "machine learning": r"machine\s+learning|\bml\b",
    "data pipelines": r"etl|data\s+pipelines?|airflow|dbt|spark",
    "observability": r"observability|opentelemetry|datadog|grafana|prometheus|tracing",
    "testing": r"unit\s+tests?|testing|pytest|jest|vitest|playwright|cypress",
    "developer tools": r"developer\s+tools|devtools|developer\s+experience|devex|dx\b|internal\s+tools|cli",
    "automation": r"automation|workflows?\s+automation|orchestration",
    "data visualization": r"data\s+visuali[sz]ation|d3(?:\.js)?|charts?",
    "electron": r"electron|tauri",
    "websockets": r"websockets?|real[-\s]time",
    # off-domain for most product/AI engineers — named so a posting built on them scores low
    "llvm": r"llvm|mlir",
    "compilers": r"compilers?",
    "cuda": r"cuda|triton\s+kernels?|gpu\s+kernels?",
    "hardware": r"asic|fpga|rtl|verilog|vhdl|silicon",
    "embedded": r"embedded\s+(?:systems?|software|c|linux|devices?)|firmware|rtos|microcontrollers?",
    "robotics": r"robotics|ros2?\b",
    "hpc": r"hpc|high[-\s]performance\s+computing|mpi",
    "ios": r"\bios\b|swiftui|objective-c",
    "android": r"android|jetpack\s+compose",
    "salesforce": r"salesforce|apex",
    "sap": r"\bsap\b|abap",
    "mainframe": r"cobol|mainframe",
    "blockchain": r"blockchain|solidity|web3|smart\s+contracts?",
}
# Off-domain stacks: a posting built on these that the profile doesn't name is a different job.
OFF_DOMAIN = frozenset(
    {"llvm", "compilers", "cuda", "hardware", "embedded", "robotics", "hpc", "ios", "android",
     "salesforce", "sap", "mainframe", "blockchain"}
)  # fmt: skip
# Table stakes nearly every engineer has whether or not a résumé spells them out: never a gap.
BASELINE = frozenset({"git", "rest", "sql", "html", "css", "testing", "ci/cd", "linux", "docker"})
_OFF_TITLE = re.compile(
    r"\b(?:compiler|kernel|gpu|silicon|chip|asic|fpga|firmware|embedded|robotics|hardware|"
    r"performance\s+modeling|simulation|ios|android|salesforce|sap|blockchain|"
    # another discipline's engineer: every word of "Design Engineer" or "Automation Engineer" is in
    # "Utility Infra Design Engineer" and "Pharmacy Process & Automation Engineer" too
    # (a word that also names a product area is matched only as the discipline: "Network Engineer",
    # never "Neural Network Engineer" or "Software Engineer, Marketing Platform")
    r"product\s+marketing|(?:marketing|sales|clinical|wireless|telecom|optical)\s+engineer|"
    r"(?<!neural\s)network(?:ing)?\s+(?:engineer|architect|administrator)|ethernet|rf|pharmacy|utility|"
    r"utilities|civil|structural|mechanical|electrical|hvac|piping|plant|manufacturing|chemical|"
    r"geotechnical|nuclear|substation|automotive|avionics|pega|servicenow|mulesoft)\b",
    re.I,
)
# Names a posting carries when a recruiter or a reposting site listed it and the employer isn't
# named: Google for Jobs (JSearch) is full of them, and their text is written to match everything.
_REPOSTER = re.compile(
    r"\b(?:jobgether|recruiting\s+from\s+scratch|remote\s+quest|keyopenings|confidential|stealth|"
    r"lensa|jobot|cybercoders|dice|crossing\s+hurdles|braintrust|toptal|turing|mercor|micro1|outlier|"
    r"mindrift|alignerr|dataannotation|afterquery|jobbex|talentify|hire\s*feed|get\s*it\s+recruit\w*|"
    r"recruit(?:ing|ment|ers?)|staffing|talent\s+(?:solutions|partners|group))\b",
    re.I,
)
# A recruiter says so in the text even when its name doesn't: "our client is a fast-growing…".
# Only the recruiter's phrasing: an employer with customers also writes "for our clients" and
# "our client success team", and those must not match.
_FOR_A_CLIENT = re.compile(
    r"\b(?:our|my)\s+client(?:'s)?\s+(?:is|has|seeks?|needs?|wants?|requires?|team|company)\b(?!\s+success)|"
    r"\b(?:our|my)\s+client,\s+an?\b|\bon\s+behalf\s+of\s+(?:our|a|my)\s+client\b|"
    r"\bfor\s+one\s+of\s+our\s+clients\b|\bclient\s+of\s+ours\b|\bconfidential\s+(?:client|company|employer)\b",
    re.I,
)
REPOSTER_CAP = 55  # under the default inbox bar: still in a search's results, never a top card
_TECH_RE = {k: re.compile(rf"(?<![\w+#.])(?:{v})(?![\w+#])", re.I) for k, v in TECH.items()}

_SENIORITY = (  # first matching band wins, so the most senior word in a title decides
    (r"\b(?:intern|internship|new\s*grad|graduate|entry[-\s]level|junior|jr\.?|apprentice)\b", 20, "junior"),
    (r"\b(?:principal|distinguished|fellow|director|head\s+of|vp|vice\s+president|chief)\b", 40, "principal+"),
    (r"\b(?:staff|lead|tech\s+lead|team\s+lead)\b", 70, "staff/lead"),
)


def terms(text: str) -> set[str]:
    """The known technologies a text names."""
    text = text or ""
    return {k for k, rx in _TECH_RE.items() if rx.search(text)}


def seniority(title: str) -> tuple[int, str]:
    for rx, score, label in _SENIORITY:
        if re.search(rx, title or "", re.I):
            return score, label
    return 100, "mid/senior"


_NOT_ANNUAL = re.compile(
    r"/\s*(?:hr|hour|month|mo|week|wk|day)\b|hourly|monthly|weekly|daily|per\s+(?:hour|month|week|day)",
    re.I,
)
_MONEY = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|K)?")


_CURRENCY = re.compile(
    r"\b(?:CAD|EUR|GBP|AUD|NZD|INR|CHF|SEK|NOK|DKK|PLN|BRL|MXN|JPY|SGD|ILS|ZAR)\b|\b(?:C|A|NZ|CA)\$|[€£₹]"
)


def foreign_currency(comp: str) -> str:
    """The currency a pay string is in when it isn't US dollars ("CAD", "EUR", "€"…), else ""."""
    m = _CURRENCY.search(comp or "")
    return m.group(0) if m else ""


def pay_range(comp: str) -> tuple[int, int] | None:
    """Annual (lo, hi) from a comp string like "USD 190,000–255,000" or "$150K - $210K". Hourly or
    monthly figures (a period label, or a top figure under $10K) aren't annual salaries: None."""
    if not comp or _NOT_ANNUAL.search(comp):
        return None
    nums = []
    for n, k in _MONEY.findall(comp):
        v = float(n.replace(",", ""))
        if k:
            v *= 1000
        if v >= 1000:
            nums.append(int(v))
    if not nums or max(nums) < 10_000:  # under $10K can't be an annual salary (a monthly figure)
        return None
    return min(nums), max(nums)


def _title_score(title: str, roles: str) -> tuple[int, str]:
    words_in = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    best, best_role = 0, ""
    for r in re.split(r"[,;\n]", roles or ""):
        words = re.findall(r"[a-z0-9]+", r.lower())
        if words:
            s = round(100 * sum(w in words_in for w in words) / len(words))
            if s > best:
                best, best_role = s, r.strip()
    return best, best_role


def score(job: dict, roles: str, profile_text: str, min_salary: int = 0) -> tuple[int, str]:
    """``(0-100 score, one-line reason)`` for a posting. ``profile_text`` is the operator's skills /
    résumé text; ``min_salary`` the annual floor (0 = no pay part)."""
    title = job.get("title") or ""
    text = " ".join(x for x in (title, job.get("description") or job.get("snippet") or "") if x)
    parts: dict[str, int] = {}
    why: list[str] = []

    have = terms(profile_text)
    want = terms(text) - (BASELINE - have)
    if len(want) >= MIN_TERMS and have:
        hit = sorted(want & have)
        miss = sorted(want - have)
        parts["stack"] = round(100 * len(hit) / len(want))
        bit = f"stack {len(hit)}/{len(want)}"
        if miss:
            bit += f" (missing {', '.join(miss[:4])}{'…' if len(miss) > 4 else ''})"
        why.append(bit)
    else:
        parts["stack"] = NEUTRAL
        why.append("stack unknown (posting too thin)" if have else "stack unknown (no skills in profile)")

    parts["seniority"], label = seniority(title)
    why.append(label)

    parts["title"], role = _title_score(title, roles)
    if role:
        why.append(f'title ~ "{role}"' + ("" if parts["title"] == 100 else f" ({parts['title']}%)"))

    if min_salary and min_salary > 0:
        comp = job.get("comp") or ""
        rng = None if foreign_currency(comp) else pay_range(comp)
        if foreign_currency(comp):
            parts["pay"] = NEUTRAL
            why.append(f"pay posted in {foreign_currency(comp)}, not compared")
        elif rng is None:
            parts["pay"] = NEUTRAL
            why.append("pay not posted")
        else:
            lo, hi = rng
            if hi < min_salary:
                parts["pay"] = 0
                why.append(f"pay tops out ${hi // 1000}K, under your ${min_salary // 1000}K floor")
            elif lo >= min_salary:
                parts["pay"] = 100
                why.append(f"pay ${lo // 1000}K+")
            else:
                parts["pay"] = 75
                why.append(f"pay ${lo // 1000}–{hi // 1000}K straddles your floor")

    off = sorted((want & OFF_DOMAIN) - have)
    m = _OFF_TITLE.search(title)
    # off-domain for THIS operator only if their profile names neither the word nor its technology
    # ("hardware", "GPU", "simulation" aren't vocabulary terms, so check the word itself too)
    off_title = (
        m and not terms(m.group(0)) & have and not re.search(rf"\b{re.escape(m.group(0))}\b", profile_text or "", re.I)
    )
    if off_title:
        why.append(f'off-domain title ("{m.group(0)}")')
    elif len(off) >= 2:
        why.append(f"off-domain stack ({', '.join(off)})")

    w = {k: WEIGHTS[k] for k in parts}
    total = sum(w.values())
    overall = round(sum(parts[k] * w[k] for k in parts) / total)
    if parts.get("pay") == 0:  # a posted range wholly under the floor is a dealbreaker, not a dent
        overall = min(overall, 40)
    thin = len(job.get("description") or "") < THIN_CHARS
    if thin:  # an excerpt can't carry a high score: there isn't enough of the posting to judge
        why.append("excerpt only")
        overall = min(overall, THIN_CAP)
    if label == "junior":  # below the level the operator works at, however well the stack fits
        overall = min(overall, 50)
    if off_title:
        overall = min(overall, 45)
    elif len(off) >= 2:
        overall = min(overall, 55)
    if _REPOSTER.search(job.get("company") or "") or _FOR_A_CLIENT.search(text):
        # the real employer is unknown, so the stack and pay can't be trusted to be one job's
        why.append("listed by a recruiter or reposter, employer not named")
        overall = min(overall, REPOSTER_CAP)
    return overall, " · ".join(why)


def profile_text() -> str:
    """The profile text the stack part matches against: skills, résumé and roles (never
    ``do_not_claim``, which lists exactly what the operator must not be matched on). Host-side only."""
    from ..record import profile as _p

    try:
        prof = _p.load_profile()
    except Exception:  # noqa: BLE001 — an unreadable profile scores every stack neutral
        return ""
    secs = prof.get("sections") or {}
    return "\n".join(str(secs.get(k) or "") for k in ("skills", "resume", "roles"))


def rate_all(jobs: list[dict], roles: str, min_salary: int = 0, profile: str | None = None) -> list[dict]:
    """Stamp ``score`` and ``reason`` on each job (in place; also returned). Loads the profile once."""
    text = profile_text() if profile is None else profile
    for j in jobs:
        s, why = score(j, roles, text, min_salary)
        j["score"] = s
        if j.get("posted"):
            why += f" · posted {str(j['posted'])[:10]}"
        j["reason"] = why
    return jobs


def min_salary(cfg: dict) -> int:
    try:
        return max(0, int(str((cfg or {}).get("min_salary") or 0).strip() or 0))
    except (TypeError, ValueError):
        return 0
