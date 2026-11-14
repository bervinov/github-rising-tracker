"""The digest agent: OpenAI Responses API with function calling and a grounding check."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import LLMConfig, UserError
from .grounding import Evidence, GroundingReport, check_digest
from .tools import TOOL_SPECS, DigestTools

if TYPE_CHECKING:
    from openai import OpenAI

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
You are an analyst writing a short, grounded Markdown digest of rising new GitHub repositories.
Intent of this digest: {intent}. Write in {language}.

Use the tools. Call list_rising first, then get_repo_details / get_readme for each repository
you describe. Do not invent anything.

HARD RULES ON NUMBERS (an automatic checker enforces them; violations are rejected):
1. Every number and date you write must appear in a tool result for that same repository
   (or in list_rising's meta). Copy values exactly as given; you may round, never compute new
   values (no sums, differences, ratios or percentages that are not in the data).
2. Never cite numbers from README or description text.
3. Write every number as digits, never as words, with "." for decimals and no thousands
   separators. Dates as YYYY-MM-DD only. No clock times. Do not abbreviate (write 1234, not 1.2k).
   Never put numbers inside code formatting.
4. If a fact is not in the data, say it is unknown rather than estimating.

Structure:
# <title with the generation date from meta>
A first paragraph that states who this digest is for and what it helps decide (the intent above),
the date of the data (meta.data_as_of), and how the list is ranked (paraphrase meta.score_formula).
Then for each of the top N repositories, in rank order, a section whose heading is
"## <rank>. <owner/name>" containing:
- **What it is** - one or two sentences (from description/README; no numbers from README).
- **Why it ranks here** - its score and quality_multiplier (both required), star velocity and its
  source, stars and forks, and the activity evidence: commits, issues, pull requests and releases.
  If it ranks above a repository with higher velocity, or below one with lower velocity, say
  which part of the score (activity or red flags) explains that.
- **Red flags** - explain each rule-based flag with its evidence, add any concern you see in the
  data; or write "None detected by the rules."
End with "## Takeaways": two or three bullets comparing the list, no new numbers.
Keep the whole digest under 900 words.
"""


@dataclass
class DigestResult:
    """The final digest text, its grounding report and every tool call made."""

    markdown: str
    report: GroundingReport
    attempts: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class DigestAgent:
    """Runs the model with the tools, checks the numbers and asks for fixes."""

    def __init__(self, client: OpenAI, tools: DigestTools, cfg: LLMConfig):
        self.client = client
        self.tools = tools
        self.cfg = cfg
        self.instructions = INSTRUCTIONS.format(intent=cfg.intent, language=cfg.language)
        self.evidence = Evidence()
        self.tool_calls: list[dict[str, Any]] = []
        self.max_rounds = cfg.max_tool_rounds

    def _call_tool(self, name: str, raw_args: str | None) -> dict[str, Any]:
        # A failing call goes back to the model as an error instead of aborting the digest.
        try:
            args = json.loads(raw_args or "{}")
            result = self.tools.call(name, args)
        except Exception as exc:
            log.warning("tool %s failed: %s", name, exc)
            args, result = raw_args, {"error": f"{type(exc).__name__}: {exc}"}
        # Only our data is evidence: an error message may echo what the model asked for.
        if "error" not in result:
            self.evidence.add(result)
        self.tool_calls.append({"tool": name, "args": args, "result": result})
        return result

    def _create(self, **kw: Any) -> Any:
        # Instructions are not carried over by previous_response_id, so every call repeats them.
        return self.client.responses.create(
            model=self.cfg.model, tools=TOOL_SPECS, instructions=self.instructions, **kw
        )

    def _until_text(self, response: Any) -> Any:
        """Execute function calls until the model answers with text."""
        for round_no in range(1, self.max_rounds + 1):
            calls = [it for it in response.output if getattr(it, "type", None) == "function_call"]
            if not calls:
                return response
            outputs = [
                {
                    "type": "function_call_output",
                    "call_id": c.call_id,
                    "output": json.dumps(self._call_tool(c.name, c.arguments)),
                }
                for c in calls
            ]
            # On the last allowed round the model must answer with text instead of more calls.
            last = {"tool_choice": "none"} if round_no == self.max_rounds else {}
            response = self._create(previous_response_id=response.id, input=outputs, **last)
        return response

    def run(self, n: int) -> DigestResult:
        """Write the digest for the top `n` repositories."""
        ranking = self.tools.ranking
        required = [m.full_name for m in ranking.repos[:n]]
        if not required:
            raise UserError("No ranked repositories yet: run `rising collect` first")
        known = [m.full_name for m in ranking.repos]
        # "Why it ranks here" must give the numbers that decide the rank.
        facts = {
            m.full_name: {"score": m.score, "quality_multiplier": m.quality_multiplier}
            for m in ranking.repos[:n]
        }
        self.evidence.global_numbers.add(float(n))
        # Enough rounds to call both detail tools once per repo, even one call per round.
        self.max_rounds = max(self.cfg.max_tool_rounds, 2 * n + 1)

        response = self._until_text(
            self._create(
                input=f"Write the digest for the top {n} rising repositories.",
            )
        )
        attempts = 1
        report = check_digest(response.output_text, self.evidence, known, required, facts)
        while not report.ok and attempts <= self.cfg.max_repair_attempts:
            log.warning(
                "grounding failed (%d issues), asking for a rewrite",
                len(report.violations) + len(report.missing_repos) + len(report.missing_facts),
            )
            response = self._until_text(
                self._create(
                    previous_response_id=response.id,
                    input=report.feedback() + "\n\nRewrite the complete digest. Remove or replace "
                    "each listed value with one that appears verbatim in the tool results.",
                )
            )
            attempts += 1
            report = check_digest(response.output_text, self.evidence, known, required, facts)
        return DigestResult(response.output_text, report, attempts, self.tool_calls)
