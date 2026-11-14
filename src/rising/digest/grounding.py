"""Grounding check: every number and date in the digest must appear in the tool results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?\b")
# A number is checked unless it is part of a word, path or version ("gpt-4", "v2", "a/1", "1.2.3").
# "999/day", "500-star", "-42", "#7", "9999/7777", "25M", "3rd" and "9999stars" are checked.
NUM_RE = re.compile(
    r"(?<![\w.@])(?<![A-Za-z_][/-])"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?P<suf>\s?%|[kKmMbB]\b|x\b|×|h\b|d\b|st\b|nd\b|rd\b|th\b)?"
    r"(?![\d_]|\.\d)"
)
MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9}
# Numbers written as words cannot be checked, so they are rejected ("one" is too common in prose).
WORD_NUM_RE = re.compile(
    r"\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    r"fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|"
    r"eighty|ninety|hundreds?|thousands?|millions?|billions?|dozens?|half|halved|twice|"
    r"double[ds]?|triple[ds]?|quarter)\b(?!-)",  # "double-check" is not a number
    re.IGNORECASE,
)
# A number followed by one of these words must equal a field of that name ("75 forks" is checked
# against fields whose name contains "forks"), not just any number of the repository.
FIELD_RE = re.compile(
    r"\s*(?:new |open |closed |merged |total |recent )?"
    r"(?P<field>(?:commit )?authors?|contributors?|stars?|forks?|commits?|issues?|PRs?|"
    r"pull requests?|releases?|days?|hours?)\b"
    r"(?P<rate>\s*/\s*day|\s+per\s+day|\s+a\s+day)?",
    re.IGNORECASE,
)
# The same words as a label before the number: "Stars: 75", "forks = 3".
LABEL_RE = re.compile(
    r"\b(?P<field>stars|forks|commits|issues|PRs|pull requests|releases|authors|contributors)"
    r"\**\s*[:=]\s*\**$",
    re.IGNORECASE,
)
FIELD_TOKENS = {
    "star": "stars",
    "fork": "forks",
    "commit": "commits",
    "issue": "issues",
    "pr": "prs",
    "pull": "prs",
    "release": "releases",
    "author": "authors",
    "contributor": "authors",
    "day": "days",
    "hour": "hours",
}
RATE = "stars/day"
# A percentage or multiplier ("35%", "2x") must match a share-like field, not any number.
PCT_KEYS = frozenset({"pct", "share", "ratio", "factor", "multiplier", "penalty"})
URL_RE = re.compile(r"\(https?://[^)\s]+\)|https?://\S+")
HEADING_RE = re.compile(r"^(?P<level>#{1,6})\s+(?P<text>.*)$")
PLAIN_NUMBER_RE = re.compile(r"[\d.,]+")
# Values under these keys are names, not measurements: they may contain digits ("gpt-4o", "v2.1").
PROTECTED_KEYS = frozenset(
    {"topics", "latest_release_tag", "url", "html_url", "homepage", "full_name"}
)


@dataclass
class Evidence:
    """Numbers and dates the agent received, per repository and global."""

    by_repo: dict[str, set[float]] = field(default_factory=dict)
    dates_by_repo: dict[str, set[str]] = field(default_factory=dict)
    # Allowed only outside repository sections: ranking metadata such as N and the score formula.
    global_numbers: set[float] = field(default_factory=set)
    global_dates: set[str] = field(default_factory=set)
    # Names and tags that contain digits; removed from the text before numbers are read.
    protected_tokens: set[str] = field(default_factory=set)
    # Values by field word ("stars", "forks", ...), per repository (None: global).
    by_field: dict[str | None, dict[str, set[float]]] = field(default_factory=dict)

    def _bucket(self, repo: str | None) -> tuple[set[float], set[str]]:
        if repo is None:
            return self.global_numbers, self.global_dates
        key = repo.lower()
        return self.by_repo.setdefault(key, set()), self.dates_by_repo.setdefault(key, set())

    def add(self, obj: Any, repo: str | None = None, key: str = "") -> None:
        """Register a tool result; values inside an object with `full_name` belong to that repo."""
        if isinstance(obj, dict):
            if isinstance(obj.get("full_name"), str):
                repo = obj["full_name"]
                self.protected_tokens.add(repo.split("/")[-1])
            for k, value in obj.items():
                if k in PROTECTED_KEYS:
                    for tok in value if isinstance(value, list) else [value]:
                        if isinstance(tok, str):
                            self.protected_tokens.add(tok)
                elif k == "signals":  # 0..1 activity signals named like counts ("forks": 0.5)
                    self.add(value, repo, "signal")
                elif k not in ("readme", "description", "score_formula"):
                    # README and description are the author's claims, not measurements; the
                    # formula's constants are not data either.
                    self.add(value, repo, k if key != "signal" else key)
        elif isinstance(obj, list):
            for item in obj:
                self.add(item, repo, key)
        elif isinstance(obj, bool) or obj is None:
            return
        elif isinstance(obj, (int, float)):
            nums, _ = self._bucket(repo)
            nums.add(float(obj))
            fields = self.by_field.setdefault(repo.lower() if repo else None, {})
            words = set(key.split("_"))
            for word in words & set(FIELD_TOKENS.values()):
                # "stars_per_day" is a rate: "215 stars" must not match it, "215 stars/day" must.
                name = RATE if word == "stars" and "per" in words else word
                fields.setdefault(name, set()).add(float(obj))
            if words & PCT_KEYS:
                fields.setdefault("%", set()).add(float(obj))
        elif isinstance(obj, str):
            nums, dates = self._bucket(repo)
            for m in DATE_RE.finditer(obj):
                dates.add(m.group(1))
            for m in NUM_RE.finditer(DATE_RE.sub(" ", obj)):
                nums.add(_value(m))
                if (m.group("suf") or "").strip() == "%":
                    fields = self.by_field.setdefault(repo.lower() if repo else None, {})
                    fields.setdefault("%", set()).add(_value(m))

    def field_values(self, repo: str | None, word: str) -> set[float]:
        """Values of fields named like `word` ("forks") in a repo's section or a general one."""
        return self.by_field.get(repo.lower() if repo else None, {}).get(word, set())

    def allowed(self, repo: str | None) -> tuple[set[float], set[str]]:
        """Numbers and dates allowed in a section about `repo`, or in a general section."""
        if repo is None:
            return self.global_numbers, self.global_dates
        key = repo.lower()
        return self.by_repo.get(key, set()), self.dates_by_repo.get(key, set())


def _field_word(text: str) -> str:
    """ "Pull requests" -> "prs", "Stars" -> "stars"."""
    word = text.lower().removeprefix("commit ").split()[0] if " author" in text.lower() else text
    return FIELD_TOKENS[word.lower().split()[0].removesuffix("s")]


def _multiplier(m: re.Match[str]) -> float:
    return MULTIPLIERS.get((m.group("suf") or "").strip().lower(), 1.0)


def _value(m: re.Match[str]) -> float:
    return float(m.group("int").replace(",", "") + (m.group("frac") or "")) * _multiplier(m)


def _matches(m: re.Match[str], allowed: set[float]) -> bool:
    """Whether the number equals an allowed value at the precision it is written with."""
    value = _value(m)
    decimals = len(m.group("frac")) - 1 if m.group("frac") else 0
    # "1.2k" must be exactly 1200: an abbreviation does not widen the tolerance.
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    candidates = [(value, tol), (-value, tol)]
    if (m.group("suf") or "").strip() == "%":
        candidates.append((value / 100, tol / 100))  # "35%" may be a stored ratio of 0.35
    return any(abs(a - c) <= t for a in allowed for c, t in candidates)


@dataclass
class Violation:
    """A number or date in the digest that is not in the data."""

    token: str
    repo: str | None
    line: str

    def __str__(self) -> str:
        where = f" (section: {self.repo})" if self.repo else ""
        return f"'{self.token}'{where} in line: {self.line.strip()[:160]}"


@dataclass
class GroundingReport:
    """Result of checking a digest."""

    checked: int = 0
    violations: list[Violation] = field(default_factory=list)
    missing_repos: list[str] = field(default_factory=list)
    missing_facts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing is unverified and every ranked repo has a complete section."""
        return not self.violations and not self.missing_repos and not self.missing_facts

    def feedback(self) -> str:
        """Instructions for the model listing what to fix."""
        parts = []
        if self.violations:
            parts.append(
                "These numbers/dates are NOT present in the retrieved data "
                "(numbers written as words must be written as digits from the data):"
            )
            parts += [f"- {v}" for v in self.violations]
        if self.missing_repos:
            parts.append(
                "These ranked repositories are missing their own '## <rank>. <owner/name>' "
                "section: " + ", ".join(self.missing_repos)
            )
        if self.missing_facts:
            parts.append("These sections must state the following values:")
            parts += [f"- {f}" for f in self.missing_facts]
        return "\n".join(parts)


def _name_pattern(evidence: Evidence, known_repos: list[str]) -> re.Pattern[str] | None:
    """One pattern for repository names and other protected tokens that contain digits."""
    tokens = [
        t
        for t in evidence.protected_tokens
        if t and any(ch.isdigit() for ch in t) and not PLAIN_NUMBER_RE.fullmatch(t)
    ]
    parts = [re.escape(r) for r in sorted(known_repos, key=len, reverse=True)]
    parts += [rf"(?<![\w-]){re.escape(t)}(?![\w-])" for t in sorted(tokens, key=len, reverse=True)]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def check_digest(
    markdown: str,
    evidence: Evidence,
    known_repos: list[str],
    required_repos: list[str] | None = None,
    required_values: dict[str, dict[str, float]] | None = None,
) -> GroundingReport:
    """Check every number and date in the digest against the evidence.

    A `#` or `##` heading that names a repository starts that repository's section; numbers in it
    must come from that repository's data. Other sections (intro, takeaways) may only use the
    ranking metadata. Deeper headings stay in the current section.
    `required_values` lists values each repository's section must state, e.g. its score.
    """
    report = GroundingReport()
    names = _name_pattern(evidence, known_repos)
    longest_first = sorted(known_repos, key=len, reverse=True)
    current: str | None = None
    sections: set[str] = set()
    stated: dict[str, list[re.Match[str]]] = {}
    for raw in markdown.splitlines():
        heading = HEADING_RE.match(raw)
        if heading and len(heading.group("level")) <= 2:
            text = heading.group("text").lower()
            current = next((r for r in longest_first if r.lower() in text), None)
            if current:
                sections.add(current.lower())
        line = URL_RE.sub(" ", raw)
        if names:
            line = names.sub(" ", line)
        allowed_nums, allowed_dates = evidence.allowed(current)
        for m in DATE_RE.finditer(line):
            report.checked += 1
            if m.group(1) not in allowed_dates:
                report.violations.append(Violation(m.group(0), current, raw))
        for m in WORD_NUM_RE.finditer(line):
            report.checked += 1
            report.violations.append(Violation(m.group(0), current, raw))
        text = DATE_RE.sub(" ", line)
        for m in NUM_RE.finditer(text):
            report.checked += 1
            if current and not heading:  # the rank in a heading does not state a value
                stated.setdefault(current.lower(), []).append(m)
            named = FIELD_RE.match(text, m.end()) or LABEL_RE.search(text, 0, m.start())
            if (m.group("suf") or "").strip().lower() in ("%", "x", "×"):  # shares and ratios
                allowed_here = evidence.field_values(current, "%")
            elif named:
                word = _field_word(named.group("field"))
                if word == "stars" and named.groupdict().get("rate"):
                    word = RATE
                allowed_here = evidence.field_values(current, word)
            else:
                allowed_here = allowed_nums
            if not _matches(m, allowed_here):
                report.violations.append(Violation(m.group(0).strip(), current, raw))
    report.missing_repos = [r for r in (required_repos or []) if r.lower() not in sections]
    for repo, values in (required_values or {}).items():
        if repo.lower() not in sections:
            continue
        found = stated.get(repo.lower(), [])
        for name, value in values.items():
            if not any(_matches(m, {value}) for m in found):
                report.missing_facts.append(f"{repo}: {name} {value}")
    return report
