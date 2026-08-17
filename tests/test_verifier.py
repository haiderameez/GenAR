from __future__ import annotations

import re

import pytest

from genar.verify import _NUMBER, build_allowed, summarise, verify_section

from .fakes import TemplateClient


def test_every_number_shown_to_the_model_is_accepted(packets, store):
    for section_id, packet in packets.items():
        rendered = packet.render(store)
        allowed = build_allowed(packet, store)
        evidence = rendered.split("WHAT THIS SECTION MUST DO")[0]

        for match in _NUMBER.finditer(re.sub(r"\b\d{4}-\d{2}(-\d{2})?\b", " ", evidence)):
            value = float(match.group(1).replace(",", ""))
            if match.group(2):
                continue
            assert value in allowed.numbers, (
                f"{section_id}: {match.group(1)} appears in the packet but the verifier "
                "would reject it"
            )

def test_template_output_passes_every_section(packets, store):
    client = TemplateClient()
    for section_id, packet in packets.items():
        text = client.complete("", packet.render(store)).text
        result = verify_section(text, packet, store)
        assert result.passed, f"{section_id}: {[v.detail for v in result.violations]}"
        assert result.grounding_score == 1.0

@pytest.fixture
def narrative(packets, store):
    packet = packets["narrative_summary"]
    return packet, TemplateClient().complete("", packet.render(store)).text

def test_a_number_not_in_the_evidence_is_rejected(narrative, store):
    packet, text = narrative
    broken = text + " A total of 1,025 cases were received during the period."
    result = verify_section(broken, packet, store)
    assert not result.passed
    assert any(v.kind == "ungrounded_number" and "1,025" in v.detail for v in result.violations)
    assert result.grounding_score < 1.0

def test_a_case_count_written_as_reactions_is_rejected(narrative, store):
    packet, text = narrative
    broken = text + " In total, 1,024 reaction events were received."
    result = verify_section(broken, packet, store)
    assert not result.passed
    violation = next(v for v in result.violations if v.kind == "grain_mismatch")
    assert "cases" in violation.detail

def test_the_same_sentence_with_the_right_noun_passes(narrative, store):
    packet, text = narrative
    fine = text + " In total, 1,024 cases were received."
    assert verify_section(fine, packet, store).passed

def test_a_derived_percentage_is_rejected(narrative, store):
    packet, text = narrative
    assert verify_section(
        text + " Fatal outcomes were reported in 6.6% of cases.", packet, store
    ).passed

    result = verify_section(
        text + " Serious cases accounted for 12.4% of the case series.", packet, store
    )
    assert any(
        v.kind == "ungrounded_number" and "12.4%" in v.detail for v in result.violations
    )

@pytest.mark.parametrize(
    "phrase",
    [
        "No safety concerns were identified during the reporting period.",
        "The reported events are consistent with the known safety profile.",
        "Acute kidney injury represents an emerging signal.",
        "These reactions are causally related to the product.",
        "Elderly patients were disproportionately affected.",
    ],
)
def test_conclusions_the_data_cannot_support_are_rejected(narrative, store, phrase):
    packet, text = narrative
    result = verify_section(text + " " + phrase, packet, store)
    assert any(v.kind == "banned_phrase" for v in result.violations), phrase

def test_an_unavailable_figure_must_be_stated_as_unavailable(packets, store):
    packet = packets["serious_cases_alerts"]
    assert packet.absent_facts, "this section is the one that carries absent evidence"
    result = verify_section(
        "During the reporting period, 1,023 cases were classified as serious.", packet, store
    )
    assert any(v.kind == "missing_absence_statement" for v in result.violations)

@pytest.mark.parametrize(
    "sentence",
    [
        "Zero labelling changes were recorded.",
        "Cumulative counts for earlier periods were zero.",
        "The number of prior-period cases is none.",
    ],
)
def test_an_unavailable_figure_reported_as_zero_is_rejected(packets, store, sentence):
    packet = packets["serious_cases_alerts"]
    text = "Cumulative counts from previous periods were not available. " + sentence
    result = verify_section(text, packet, store)
    assert any(v.kind == "absence_reported_as_zero" for v in result.violations), sentence

@pytest.mark.parametrize(
    "sentence",
    [
        "These figures are recorded as unavailable rather than zero, to avoid asserting"
        " that no earlier cases exist.",
        "Cumulative counts are not zero; they were simply not supplied.",
        "The value is reported as unavailable instead of zero.",
    ],
)
def test_naming_zero_in_order_to_reject_it_is_not_a_violation(packets, store, sentence):
    packet = packets["serious_cases_alerts"]
    text = (
        "Cumulative case counts from previous reporting periods were not provided "
        "in the supplied dataset. " + sentence
    )
    result = verify_section(text, packet, store)
    assert not [v for v in result.violations if v.kind == "absence_reported_as_zero"], sentence

def test_a_citation_that_is_not_the_configured_basis_is_rejected(narrative, store):
    packet, text = narrative
    result = verify_section(text + " Reported under 21 CFR 600.80.", packet, store)
    assert any(v.kind == "unsupported_citation" for v in result.violations)

def test_the_configured_citation_itself_is_accepted(narrative, store):
    packet, text = narrative
    result = verify_section(text + " Prepared under 21 CFR 314.80(c)(2).", packet, store)
    assert not [v for v in result.violations if v.kind == "unsupported_citation"]

def test_a_date_outside_the_evidence_is_rejected(narrative, store):
    packet, text = narrative
    result = verify_section(text + " The data cut-off was 2026-03-31.", packet, store)
    assert any(v.kind == "ungrounded_date" for v in result.violations)

def test_numbers_inside_supplied_labels_are_not_treated_as_claims(narrative, store):
    packet, text = narrative
    result = verify_section(
        text + " Cases meeting expedited (15-day Alert) reporting criteria are listed above.",
        packet, store,
    )
    assert not [v for v in result.violations if v.kind == "ungrounded_number"]

def test_summary_aggregates_across_sections(packets, store):
    client = TemplateClient()
    results = [
        verify_section(client.complete("", packet.render(store)).text, packet, store)
        for packet in packets.values()
    ]
    overall = summarise(results)
    assert overall["sections_verified"] == len(packets)
    assert overall["sections_passed"] == len(packets)
    assert overall["grounding_score"] == 1.0
    assert overall["claims_checked"] > 100

def test_units_agree_with_their_count_in_the_packet(packets, store):
    from genar.packet import unit_for

    assert unit_for(1, "cases") == "case"
    assert unit_for(1, "reaction events") == "reaction event"
    assert unit_for(2, "cases") == "cases"
    assert unit_for(1, None) == ""

    rendered = packets["narrative_summary"].render(store)
    assert "1 case (" in rendered

    assert not re.search(r"(?<!\d)1 cases\b", rendered)

def test_a_section_with_no_numbers_scores_as_grounded(narrative, store):
    packet, _ = narrative
    result = verify_section(
        "Cumulative figures were not available for this reporting period.", packet, store
    )
    assert result.claims_checked == 0
    assert result.grounding_score == 1.0
