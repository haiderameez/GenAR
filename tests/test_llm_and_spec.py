from __future__ import annotations

import json

import pytest

from genar.config import LLMSettings, load_dotenv
from genar.errors import (
    ConfigurationError,
    EvidenceNotFoundError,
    QuotaExceededError,
)
from genar.llm import DailyBudget, RateLimiter, prompt_digest
from genar.spec import (
    CLAIM_DERIVED,
    check_against_analyses,
    configuration_facts,
    load_spec,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

def test_the_first_call_is_not_delayed():
    clock = FakeClock()
    limiter = RateLimiter(6.0)
    assert limiter.wait(sleep=clock.sleep, now=clock.monotonic) == 0.0
    assert clock.slept == []

def test_calls_are_spaced_by_the_configured_interval():
    clock = FakeClock()
    limiter = RateLimiter(6.0)
    limiter.wait(sleep=clock.sleep, now=clock.monotonic)
    paused = limiter.wait(sleep=clock.sleep, now=clock.monotonic)
    assert paused == pytest.approx(6.0)

def test_a_slow_caller_is_not_delayed_further():
    clock = FakeClock()
    limiter = RateLimiter(6.0)
    limiter.wait(sleep=clock.sleep, now=clock.monotonic)
    clock.now += 30.0
    assert limiter.wait(sleep=clock.sleep, now=clock.monotonic) == 0.0

def test_settings_convert_requests_per_minute_to_an_interval():
    assert LLMSettings(requests_per_minute=10).min_seconds_between_calls == pytest.approx(6.0)
    assert LLMSettings(requests_per_minute=0).min_seconds_between_calls == 0.0

def test_the_prompt_fingerprint_covers_model_rules_and_evidence():
    base = prompt_digest("m1", "system", "prompt")
    assert base != prompt_digest("m2", "system", "prompt")
    assert base != prompt_digest("m1", "different rules", "prompt")
    assert base != prompt_digest("m1", "system", "different evidence")
    assert base == prompt_digest("m1", "system", "prompt")

def test_every_call_reaches_the_provider(monkeypatch):
    settings = LLMSettings()
    calls = []

    class Recorder:
        name = "gemini"
        model = settings.model

        def complete(self, system, prompt):
            calls.append(prompt)
            from genar.llm import Completion

            return Completion(text="written", model=self.model, prompt_sha256="x")

    client = Recorder()
    client.complete("rules", "packet")
    client.complete("rules", "packet")
    assert len(calls) == 2

def test_a_completion_carries_no_cache_state():
    from genar.llm import Completion

    fields = set(Completion.__dataclass_fields__)
    assert fields == {"text", "model", "prompt_sha256"}

def test_a_daily_quota_error_is_not_retried():
    from genar.llm import _is_daily_quota_exhausted, _is_retryable

    daily = Exception(
        "429 RESOURCE_EXHAUSTED 'quotaId': "
        "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'quotaValue': '20'"
    )
    assert _is_daily_quota_exhausted(daily)
    assert not _is_retryable(daily)

def test_a_per_minute_limit_is_still_retried():
    from genar.llm import _is_daily_quota_exhausted, _is_retryable

    per_minute = Exception(
        "429 RESOURCE_EXHAUSTED 'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel'"
    )
    assert not _is_daily_quota_exhausted(per_minute)
    assert _is_retryable(per_minute)

def test_transient_server_errors_are_still_retried():
    from genar.llm import _is_retryable

    assert _is_retryable(Exception("503 UNAVAILABLE. high demand"))
    assert not _is_retryable(Exception("400 INVALID_ARGUMENT"))

def test_the_daily_budget_refuses_the_call_that_would_exceed_it(tmp_path):
    budget = DailyBudget(limit=2, model="m1", directory=tmp_path)
    budget.claim()
    budget.claim()
    assert budget.remaining == 0
    with pytest.raises(QuotaExceededError, match="daily request budget for m1 exhausted"):
        budget.claim()

def test_the_budget_is_counted_per_model(tmp_path):
    first = DailyBudget(limit=2, model="m1", directory=tmp_path)
    first.claim()
    first.claim()
    assert first.remaining == 0

    second = DailyBudget(limit=2, model="m2", directory=tmp_path)
    assert second.used == 0
    second.claim()
    assert second.remaining == 1
    assert first.used == 2

def test_the_budget_survives_a_restart(tmp_path):
    DailyBudget(limit=5, model="m1", directory=tmp_path).claim()
    assert DailyBudget(limit=5, model="m1", directory=tmp_path).used == 1

def test_a_budget_from_another_day_does_not_count(tmp_path):
    (tmp_path / "daily_budget.json").write_text(
        json.dumps({"date": "1999-01-01", "models": {"m1": 500}}), encoding="utf-8"
    )
    assert DailyBudget(limit=5, model="m1", directory=tmp_path).used == 0

def test_the_template_client_satisfies_the_llm_protocol():
    from .fakes import TemplateClient

    client = TemplateClient()
    completion = client.complete("rules", "[cases.total] Total cases: 1,024 cases")
    assert completion.text
    assert completion.model == client.model
    assert client.calls_made == 1

def test_dotenv_reads_keys_comments_quotes_and_export(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "\n"
        "GENAR_TEST_PLAIN=abc123\n"
        'GENAR_TEST_QUOTED="with spaces"\n'
        "export GENAR_TEST_EXPORTED=xyz\n"
        "GENAR_TEST_EMPTY=\n"
        "not_a_pair\n",
        encoding="utf-8",
    )
    for name in ("GENAR_TEST_PLAIN", "GENAR_TEST_QUOTED", "GENAR_TEST_EXPORTED", "GENAR_TEST_EMPTY"):
        monkeypatch.delenv(name, raising=False)

    applied = load_dotenv(path)

    import os

    assert os.environ["GENAR_TEST_PLAIN"] == "abc123"
    assert os.environ["GENAR_TEST_QUOTED"] == "with spaces"
    assert os.environ["GENAR_TEST_EXPORTED"] == "xyz"
    assert os.environ["GENAR_TEST_EMPTY"] == ""
    assert "not_a_pair" not in applied

def test_the_real_environment_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("GENAR_TEST_PRECEDENCE=from_file\n", encoding="utf-8")
    monkeypatch.setenv("GENAR_TEST_PRECEDENCE", "from_shell")

    applied = load_dotenv(path)

    import os

    assert os.environ["GENAR_TEST_PRECEDENCE"] == "from_shell"
    assert "GENAR_TEST_PRECEDENCE" not in applied

def test_a_missing_dotenv_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nothing-here") == {}

def test_the_pader_spec_loads_and_declares_its_evidence(spec):
    assert spec.report_type == "pader"
    assert len(spec.sections) == 9
    assert spec.llm_section_count == 5
    assert "cases.total" in spec.required_fact_ids

def test_every_declared_fact_has_a_producer(spec):
    available = {
        *__import__("genar.analyses", fromlist=["x"]).registered_fact_ids(),
        *(fact.id for fact in configuration_facts(spec)),
        "meta.data_quality",
        "meta.dataset_provenance",
    }
    check_against_analyses(spec, available)

def test_a_spec_asking_for_evidence_nobody_produces_fails_early(tmp_path):
    from genar.analyses import registered_fact_ids

    path = tmp_path / "typo.yaml"
    path.write_text(
        "report_type: typo\ntitle: t\nproduct: p\nsections:\n"
        "  - id: a\n    generator: llm\n    instructions: go\n"
        "    requires: [cases.total, cases.imaginary]\n",
        encoding="utf-8",
    )
    with pytest.raises(EvidenceNotFoundError, match="cases.imaginary"):
        check_against_analyses(load_spec(path), set(registered_fact_ids()))

def test_no_section_is_granted_interpretation(spec):
    assert {section.claim_level for section in spec.sections} <= {"observed", CLAIM_DERIVED}

def test_deterministic_sections_name_a_renderer(spec):
    from genar.render import renderer_names

    for section in spec.sections:
        if not section.uses_llm:
            assert section.renderer in renderer_names(), section.id

def test_model_written_sections_carry_instructions(spec):
    for section in spec.sections:
        if section.uses_llm:
            assert section.instructions.strip(), section.id
            assert section.requires, section.id

@pytest.mark.parametrize(
    "broken, message",
    [
        ("report_type: x\ntitle: t\nproduct: p\nsections: []", "missing required key 'sections'"),
        (
            "report_type: x\ntitle: t\nproduct: p\nsections:\n  - heading: h\n    generator: llm",
            "missing 'id'",
        ),
        (
            "report_type: x\ntitle: t\nproduct: p\nsections:\n  - id: a\n    generator: telepathy",
            "generator must be one of",
        ),
        (
            "report_type: x\ntitle: t\nproduct: p\nsections:\n  - id: a\n    generator: deterministic",
            "must name a renderer",
        ),
        (
            "report_type: x\ntitle: t\nproduct: p\nsections:\n  - id: a\n    generator: llm",
            "must carry instructions",
        ),
        (
            "report_type: x\ntitle: t\nproduct: p\nsections:\n"
            "  - id: a\n    generator: llm\n    instructions: go\n    claim_level: omniscient",
            "claim_level must be one of",
        ),
    ],
)
def test_configuration_errors_surface_before_any_call(tmp_path, broken, message):
    path = tmp_path / "broken.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_spec(path)

def test_duplicate_section_ids_are_rejected(tmp_path):
    path = tmp_path / "dupe.yaml"
    path.write_text(
        "report_type: x\ntitle: t\nproduct: p\nsections:\n"
        "  - id: a\n    generator: llm\n    instructions: go\n"
        "  - id: a\n    generator: llm\n    instructions: go\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="duplicate section id"):
        load_spec(path)

def test_configuration_facts_are_marked_as_coming_from_configuration(spec):
    facts = {fact.id: fact for fact in configuration_facts(spec)}
    assert facts["product.name"].value == "Bisoprolol"
    assert "report configuration" in facts["product.name"].method
    assert facts["meta.regulatory_basis"].value == "21 CFR 314.80(c)(2)"
