from __future__ import annotations

import pytest

from genar.errors import EvidenceNotFoundError, FactError
from genar.facts import (
    GRAIN_CASE,
    GRAIN_EVENT,
    GRAIN_NONE,
    SCOPE_DEDUPLICATED,
    STATUS_NOT_PROVIDED,
    Distribution,
    Fact,
    FactStore,
    format_percent,
)


def test_every_computed_fact_declares_a_grain(facts):
    for fact in facts:
        assert fact.grain in {GRAIN_CASE, GRAIN_EVENT, GRAIN_NONE}

def test_event_grain_facts_declare_a_scope(facts):
    for fact in facts:
        if fact.grain == GRAIN_EVENT:
            assert fact.scope in {"deduplicated", "all_rows"}, fact.id
        else:
            assert fact.scope is None, fact.id

def test_fact_ids_agree_with_their_namespace(facts):
    for fact in facts:
        namespace = fact.id.split(".")[0]
        if namespace == "cases":
            assert fact.grain == GRAIN_CASE, fact.id
        elif namespace == "events":
            assert fact.grain == GRAIN_EVENT, fact.id
        elif namespace in {"period", "absent"}:
            assert fact.grain == GRAIN_NONE, fact.id

def test_unavailable_facts_carry_no_value_and_no_cases(facts):
    absent = [fact for fact in facts if not fact.is_available]
    assert absent, "the dataset cannot answer everything the regulation asks for"
    for fact in absent:
        assert fact.value is None, fact.id
        assert fact.case_ids == (), fact.id
        assert "not " in fact.absence_phrase

def test_every_computed_counting_fact_cites_its_cases(facts):
    for fact in facts:
        if fact.is_available and fact.grain != GRAIN_NONE:
            assert fact.case_ids, fact.id
            assert fact.unit, fact.id

def test_grain_must_match_the_namespace():
    with pytest.raises(FactError, match="case-grain"):
        Fact(id="cases.total", label="x", value=1, grain=GRAIN_EVENT,
             scope=SCOPE_DEDUPLICATED, unit="reaction events", method="m")

def test_event_facts_without_a_scope_are_rejected():
    with pytest.raises(FactError, match="must declare scope"):
        Fact(id="events.total", label="x", value=1, grain=GRAIN_EVENT,
             unit="reaction events", method="m")

def test_absent_facts_cannot_carry_a_value():
    with pytest.raises(FactError, match="requires value=None"):
        Fact(id="absent.history_of_actions", label="x", value=0, grain=GRAIN_NONE,
             method="m", status=STATUS_NOT_PROVIDED)

def test_counting_facts_must_declare_a_unit():
    with pytest.raises(FactError, match="must declare a unit"):
        Fact(id="cases.total", label="x", value=1, grain=GRAIN_CASE, method="m")

def test_fact_store_rejects_duplicate_ids():
    store = FactStore([Fact(id="cases.total", label="x", value=1, grain=GRAIN_CASE,
                            unit="cases", method="m")])
    with pytest.raises(FactError, match="duplicate fact id"):
        store.add(Fact(id="cases.total", label="y", value=2, grain=GRAIN_CASE,
                       unit="cases", method="m"))

def test_fact_store_names_every_missing_requirement_at_once(facts):
    with pytest.raises(EvidenceNotFoundError) as excinfo:
        facts.require(["cases.total", "cases.nonexistent", "events.imaginary"])
    message = str(excinfo.value)
    assert "cases.nonexistent" in message and "events.imaginary" in message
    assert "cases.total" not in message

def test_percentages_use_one_rounding_rule_everywhere():
    assert format_percent(1023, 1024) == "99.9%"
    assert format_percent(1, 1024) == "0.1%"
    assert format_percent(1, 0) == "not calculable"

def test_distribution_keeps_its_denominator():
    dist = Distribution.from_counts({"a": 3, "b": 1}, total=4)
    assert dist.items == (("a", 3), ("b", 1))
    assert dist.total == 4
    assert dist.count_for("missing") == 0

def test_distribution_respects_a_fixed_order():
    dist = Distribution.from_counts({"old": 1, "young": 9}, total=10, order=("young", "old"))
    assert dist.labels == ("young", "old")
