import json
from pathlib import Path
from types import SimpleNamespace

from rising.digest.agent import DigestAgent
from rising.digest.grounding import Evidence, check_digest
from rising.digest.service import generate_digest
from rising.digest.tools import DigestTools
from rising.domain.ranking import rank

from .conftest import NOW


def _evidence(histories, settings):
    ranking = rank(histories, settings, NOW)
    ev = Evidence()
    ev.add(
        {
            "meta": {"n": 2, "generated_at": "2026-11-13T12:00:00Z"},
            "repos": [m.model_dump(mode="json") for m in ranking.repos],
        }
    )
    ev.add({"full_name": "hype/tool", "readme": "Benchmarks: 37x faster, 9000 users"})
    return ev, [m.full_name for m in ranking.repos]


def test_grounding_accepts_values_from_data_with_rounding(histories, settings):
    ev, known = _evidence(histories, settings)
    md = (
        "# Digest 2026-11-13\n## 1. good/agent\nGained 240 stars in 24.0 hours "
        "(213.3 per day lifetime), 35 forks, 6 recent authors, created 2026-11-10.\n"
        "## 2. hype/tool\n1500 stars, 2 forks, 1300 stars on 2026-11-11 (86.67%).\n"
    )
    report = check_digest(md, ev, known, known)
    assert report.ok, report.feedback()
    assert report.checked == 14  # 3 dates and 11 numbers, ranks included


def test_grounding_rejects_invented_readme_and_cross_repo_numbers(histories, settings):
    ev, known = _evidence(histories, settings)
    md = (
        "## 1. good/agent\nAbout 250 stars a day. It has 1500 stars.\n"
        "## 2. hype/tool\nClaims 37x faster.\n"
    )
    assert {v.token for v in check_digest(md, ev, known, known).violations} == {
        "250",
        "1500",
        "37x",
    }


def test_grounding_ignores_digits_in_names_and_urls(histories, settings):
    ev, known = _evidence(histories, settings)
    ev.protected_tokens.add("gpt-4o")
    md = (
        "## 1. good/agent\nWorks with gpt-4o, see [repo](https://github.com/good/agent2).\n"
        "## 2. hype/tool\nok\n"
    )
    assert check_digest(md, ev, known, known).ok


def test_grounding_checks_numbers_in_code_too(histories, settings):
    ev, known = _evidence(histories, settings)
    md = "## 1. good/agent\nIt has `9999` stars.\n```\nforks: 7777\n```\n## 2. hype/tool\nok\n"
    assert {v.token for v in check_digest(md, ev, known, known).violations} == {"9999", "7777"}


def test_grounding_checks_numbers_glued_to_words_and_signs(histories, settings):
    ev, known = _evidence(histories, settings)
    md = (
        "## 1. good/agent\nGains 999/day, a 37-day streak, a 500-star jump, down -42 stars.\n"
        "Built for gpt-4 and v2.\n## 2. hype/tool\nok\n"
    )
    tokens = {v.token for v in check_digest(md, ev, known, known).violations}
    assert tokens == {"999", "37", "500", "42"}


def test_grounding_closes_known_loopholes(histories, settings):
    ev, known = _evidence(histories, settings)
    ev.add({"full_name": "game/2048", "rank": 9})
    lines = {
        "Retention is 73% and 99% of users love it.": {"73%", "99%"},
        "Stars grew 240% (240 is a star count, not a share).": {"240%"},
        "It is 35x faster and 35 days old (35 is the fork count).": {"35x", "35"},
        "It has 25M stars and 12.9M views.": {"25M", "12.9M"},
        "It is the 9999th fastest and gained 9999/7777 stars.": {"9999th", "9999", "7777"},
        "### Details\n1500 stars.": {"1500"},
        "It ranks #7 with 9999stars.": {"7", "9999"},
        "It has 2048 stars.": {"2048"},
    }
    for text, expected in lines.items():
        md = f"## 1. good/agent\n{text}\n## 2. hype/tool\nok\n"
        found = {v.token for v in check_digest(md, ev, known, known).violations}
        assert found == expected, text


def test_grounding_keeps_meta_and_repo_numbers_in_their_own_sections(histories, settings):
    ev, known = _evidence(histories, settings)
    ev.add({"score_formula": "0.2 + 0.8 x activity, 10 commits = full"})
    md = (
        "Top 2 repositories.\n## 1. good/agent\nIt has 10 contributors.\n"
        "## 2. hype/tool\nok\n## Takeaways\n- One repo has 1500 stars.\n"
    )
    assert {v.token for v in check_digest(md, ev, known, known).violations} == {"10", "1500"}


def test_grounding_rejects_numbers_written_as_words(histories, settings):
    ev, known = _evidence(histories, settings)
    md = (
        "## 1. good/agent\nIt gained twelve thousand stars from one author, twice the forks.\n"
        "Worth a double-check.\n## 2. hype/tool\nok\n"
    )
    assert {v.token for v in check_digest(md, ev, known, known).violations} == {
        "twelve",
        "thousand",
        "twice",
    }


def test_grounding_ties_a_number_to_the_field_it_names(histories, settings):
    ev, known = _evidence(histories, settings)
    swapped = "## 1. good/agent\nIt has 35 stars and 640 forks.\n## 2. hype/tool\nok\n"
    assert {v.token for v in check_digest(swapped, ev, known, known).violations} == {"35", "640"}
    right = "## 1. good/agent\nIt has 640 stars and 35 forks.\n## 2. hype/tool\nok\n"
    assert check_digest(right, ev, known, known).ok


def test_grounding_reads_commit_authors_and_labels_before_numbers(histories, settings):
    ev, known = _evidence(histories, settings)
    ok = "## 1. good/agent\nIt has 6 recent commit authors. Stars: 640. Forks: **35**.\n"
    assert check_digest(ok + "## 2. hype/tool\nok\n", ev, known, known).ok
    bad = "## 1. good/agent\nStars: 35. Forks: 640. It has zero issues.\n## 2. hype/tool\nok\n"
    assert {v.token for v in check_digest(bad, ev, known, known).violations} == {
        "35",
        "640",
        "zero",
    }


def test_grounding_does_not_take_activity_signals_or_formula_constants_as_counts(
    histories, settings
):
    ev, known = _evidence(histories, settings)
    ev.add({"meta": {"score_formula": "0.2 + 0.8 x activity, 3 issues = full"}})
    md = (
        "Top 2; 3 of them are clones.\n## 1. good/agent\nIt has 1 fork and 1 commit.\n"
        "## 2. hype/tool\nok\n"
    )
    assert [v.token for v in check_digest(md, ev, known, known).violations] == ["3", "1", "1"]


def test_grounding_tells_a_star_count_from_a_star_rate(histories, settings):
    ev, known = _evidence(histories, settings)
    ok = "## 1. good/agent\n640 stars, growing 240.0 stars/day.\n## 2. hype/tool\nok\n"
    assert check_digest(ok, ev, known, known).ok
    bad = (
        "## 1. good/agent\nIt has 213.33 stars and gains 640 stars per day.\n## 2. hype/tool\nok\n"
    )
    assert {v.token for v in check_digest(bad, ev, known, known).violations} == {"213.33", "640"}


def test_grounding_does_not_trust_numbers_in_a_repo_description(histories, settings):
    ev, known = _evidence(histories, settings)
    ev.add({"full_name": "good/agent", "description": "10x faster, used by 5000 teams"})
    md = "## 1. good/agent\nUsed by 5000 teams.\n## 2. hype/tool\nok\n"
    assert [v.token for v in check_digest(md, ev, known, known).violations] == ["5000"]


def test_grounding_abbreviations_must_be_exact(histories, settings):
    ev, known = _evidence(histories, settings)
    md = "## 1. good/agent\nok\n## 2. hype/tool\nAbout 1k stars, exactly 1.5k stars.\n"
    assert {v.token for v in check_digest(md, ev, known, known).violations} == {"1k"}


def test_grounding_does_not_accept_rank_numbers_as_facts(histories, settings):
    ev, known = _evidence(histories, settings)
    md = "## 1. good/agent\n47 contributors and 33 open issues.\n## 2. hype/tool\nok\n"
    assert {v.token for v in check_digest(md, ev, known, known).violations} == {"47", "33"}


def test_grounding_requires_a_section_per_ranked_repo(histories, settings):
    ev, known = _evidence(histories, settings)
    md = "## 1. good/agent\nok\n## Takeaways\n- hype/tool is hollow.\n"
    assert check_digest(md, ev, known, known).missing_repos == ["hype/tool"]


class ScriptedOpenAI:
    """Responses API stand-in: first a tool call, then the scripted texts in order."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.requests = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        n = len(self.requests)
        if n == 1:
            call = SimpleNamespace(
                type="function_call",
                name="list_rising",
                arguments=json.dumps({"n": 2}),
                call_id="c1",
            )
            return SimpleNamespace(id=f"r{n}", output=[call], output_text="")
        return SimpleNamespace(
            id=f"r{n}", output=[SimpleNamespace(type="message")], output_text=self.texts.pop(0)
        )


BAD = "## 1. good/agent\n9999 stars!\n## 2. hype/tool\n1500 stars.\n"
GOOD_TEXT = (
    "## 1. good/agent\nScore 240.0 (quality 1.0), 640 stars.\n"
    "## 2. hype/tool\nScore 76.8 (quality 0.128), 1500 stars.\n"
)


def test_agent_repairs_ungrounded_digest(seeded, settings, histories):
    client = ScriptedOpenAI([BAD, GOOD_TEXT])
    tools = DigestTools(seeded, rank(histories, settings, NOW), None, 0)
    result = DigestAgent(client, tools, settings.llm).run(2)
    assert result.report.ok and result.attempts == 2
    assert client.requests[1]["input"][0]["type"] == "function_call_output"
    assert "9999" in client.requests[2]["input"]
    assert all("HARD RULES ON NUMBERS" in r["instructions"] for r in client.requests)


class EndlessToolCalls:
    """A model that keeps calling tools until it is told not to."""

    def __init__(self):
        self.requests = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.requests.append(kw)
        if kw.get("tool_choice") == "none":
            return SimpleNamespace(id="final", output=[], output_text=GOOD_TEXT)
        call = SimpleNamespace(
            type="function_call", name="list_rising", arguments='{"n": 2}', call_id="c"
        )
        return SimpleNamespace(id=f"r{len(self.requests)}", output=[call], output_text="")


def test_agent_forces_a_text_answer_after_max_tool_rounds(seeded, settings, histories):
    client = EndlessToolCalls()
    tools = DigestTools(seeded, rank(histories, settings, NOW), None, 0)
    agent = DigestAgent(client, tools, settings.llm)
    result = agent.run(2)
    assert result.report.ok
    assert len(client.requests) == settings.llm.max_tool_rounds + 1
    assert client.requests[-1]["tool_choice"] == "none"


class FailingTools(DigestTools):
    def call(self, name, args):
        if name == "get_readme" and args["full_name"] == "good/agent":
            raise RuntimeError("GitHub is down")
        return super().call(name, args)


def test_a_failing_tool_is_reported_to_the_model(seeded, settings, histories):
    tools = FailingTools(seeded, rank(histories, settings, NOW), None, 0)
    agent = DigestAgent(ScriptedOpenAI([GOOD_TEXT]), tools, settings.llm)
    assert agent._call_tool("get_readme", '{"full_name": "good/agent"}') == {
        "error": "RuntimeError: GitHub is down"
    }
    assert agent._call_tool("list_rising", "{not json")["error"].startswith("JSONDecodeError")
    assert agent._call_tool("get_readme", '{"full_name": "zz/5000x"}') == {
        "error": "unknown repository zz/5000x"
    }
    assert not agent.evidence.global_numbers and not agent.evidence.protected_tokens


def test_list_rising_does_not_whitelist_rank_numbers(seeded, settings, histories):
    tools = DigestTools(seeded, rank(histories, settings, NOW), None, 0)
    agent = DigestAgent(ScriptedOpenAI([GOOD_TEXT]), tools, settings.llm)
    agent.run(2)
    assert {33.0, 47.0}.isdisjoint(agent.evidence.global_numbers)


def test_generate_digest_writes_files_and_records_run(seeded, db_settings):
    result, path = generate_digest(seeded, db_settings, ScriptedOpenAI([BAD] * 3), None, 2, NOW)
    assert not result.report.ok and result.attempts == 3
    text = path.read_text()
    assert "Grounding check: FAILED" in text and "9999" in text.split("Unverified values")[1]
    evidence = json.loads(path.with_suffix(".evidence.json").read_text())
    assert evidence["tool_calls"][0]["tool"] == "list_rising"
    assert seeded.runs(1)[0].kind == "digest" and seeded.runs(1)[0].status == "failed"


def test_tools_clamp_n_and_report_unknown_names(seeded, settings, histories):
    tools = DigestTools(seeded, rank(histories, settings, NOW), None, 0)
    assert tools.call("list_rising", {"n": 500})["meta"]["n"] == 50
    assert tools.call("get_repo_details", {"full_name": "nope/nope"}) == {
        "error": "unknown repository nope/nope"
    }
    assert tools.call("delete_everything", {}) == {"error": "unknown tool delete_everything"}
    assert tools.call("get_readme", {"full_name": "GOOD/agent"}) == {
        "full_name": "good/agent",
        "readme": None,
    }


def test_grounding_skips_version_numbers(histories, settings):
    ev, known = _evidence(histories, settings)
    md = "## 1. good/agent\nNeeds Python 3.12.1 or newer.\n## 2. hype/tool\nok\n"
    assert check_digest(md, ev, known, known).ok


def test_grounding_requires_each_section_to_state_its_score(histories, settings):
    ev, known = _evidence(histories, settings)
    facts = {"good/agent": {"score": 240.0, "quality_multiplier": 1.0}}
    without = "## 1. good/agent\n640 stars.\n## 2. hype/tool\nok\n"
    report = check_digest(without, ev, known, known, facts)
    assert report.missing_facts == ["good/agent: score 240.0", "good/agent: quality_multiplier 1.0"]
    assert "must state" in report.feedback()
    with_score = "## 1. good/agent\nScore 240 with quality 1.0.\n## 2. hype/tool\nok\n"
    assert check_digest(with_score, ev, known, known, facts).ok


def test_the_example_digest_passes_the_grounding_check_on_its_evidence():
    examples = Path(__file__).parent.parent / "examples"
    calls = json.loads((examples / "digest-2026-11-14.evidence.json").read_text())["tool_calls"]
    ev = Evidence()
    for call in calls:
        ev.add(call["result"])
    listed = calls[0]["result"]
    ev.global_numbers.add(float(listed["meta"]["n"]))
    known = [r["full_name"] for r in listed["repos"]]
    facts = {
        r["full_name"]: {k: r[k] for k in ("score", "quality_multiplier")} for r in listed["repos"]
    }
    md = (examples / "digest-2026-11-14.md").read_text().split("\n---\n")[0]
    report = check_digest(md, ev, known, known, facts)
    assert report.ok, report.feedback()
