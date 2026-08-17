from __future__ import annotations

from genar.facts import Distribution
from genar.loader import age_in_years, parse_e2b_date, split_meddra_terms

def test_dates_parse_from_both_excel_floats_and_csv_text():
    from datetime import date

    assert parse_e2b_date(20241227.0) == date(2024, 12, 27)
    assert parse_e2b_date("20241227") == date(2024, 12, 27)
    assert parse_e2b_date("20241227.0") == date(2024, 12, 27)
    assert parse_e2b_date("2.0241227E7") is None
    assert parse_e2b_date("") is None
    assert parse_e2b_date("20241350") is None

def test_identifiers_are_the_same_from_excel_and_csv():
    from genar.loader import _text

    assert _text(24780403.0) == "24780403"
    assert _text("24780403.0") == "24780403"
    assert _text("24780403") == "24780403"
    assert _text("Hallucination, visual") == "Hallucination, visual"
    assert _text("2.5") == "2.5"


def test_rows_resolve_to_cases(dataset):
    assert len(dataset.all_rows) == 1068
    assert len(dataset.cases) == 1024
    assert dataset.superseded_row_count == 44

def test_one_row_per_case_after_dedup(dataset):
    ids = [case.case_id for case in dataset.cases]
    assert len(ids) == len(set(ids))

def test_dedup_keeps_the_highest_version(dataset):
    by_id: dict[str, float] = {}
    for row in dataset.all_rows:
        by_id[row.case_id] = max(by_id.get(row.case_id, 0.0), row.version)
    for case in dataset.cases:
        assert case.version == by_id[case.case_id]

def test_seriousness_split(facts):

    assert facts.get("cases.total").value == 1024
    assert facts.get("cases.serious").value == 1023
    assert facts.get("cases.non_serious").value == 1

def test_expedited_and_fatal_counts(facts):
    assert facts.get("cases.expedited").value == 1023
    assert facts.get("cases.fatal").value == 68

def test_reporting_period(facts):
    assert facts.get("period.start").value == "2024-12-27"
    assert facts.get("period.end").value == "2025-12-26"
    assert facts.get("period.days").value == 365

def test_report_type_mix(facts):

    types: Distribution = facts.get("cases.by_report_type").value
    assert types.count_for("report from study") == 10
    assert types.count_for("spontaneous report") == 1014
    assert sum(types.counts) == 1024

def test_sex_distribution_sums_to_the_case_total(facts):
    sexes: Distribution = facts.get("cases.by_sex").value
    assert sexes.count_for("female") == 503
    assert sexes.count_for("male") == 493
    assert sexes.count_for("unknown") == 28
    assert sum(sexes.counts) == 1024

def test_meddra_terms_containing_commas_survive_the_split():
    assert split_meddra_terms("Nightmare,Acute kidney injury,Hallucination,visual") == [
        "Nightmare",
        "Acute kidney injury",
        "Hallucination, visual",
    ]
    assert split_meddra_terms("Hallucinations,mixed,Hypokalaemia") == [
        "Hallucinations, mixed",
        "Hypokalaemia",
    ]
    assert split_meddra_terms("") == []

def test_reaction_lists_align_with_outcome_lists_on_every_row(dataset):
    assert [row.case_id for row in dataset.all_rows if not row.reactions_aligned] == []

def test_phantom_reaction_terms_do_not_exist(facts):
    terms = {label for label, _ in facts.get("cases.top_reactions").value.items}
    assert not terms & {"visual", "auditory", "mixed"}

def test_event_totals_at_both_scopes(facts):
    assert facts.get("events.total_all_rows").value == 3642
    assert facts.get("events.total").value == 3423
    assert facts.get("events.total_all_rows").scope == "all_rows"
    assert facts.get("events.total").scope == "deduplicated"

def test_leading_reactions_by_case_count(facts):
    top: Distribution = facts.get("cases.top_reactions").value
    assert top.count_for("Acute kidney injury") == 80
    assert top.count_for("Hypotension") == 46
    assert top.count_for("Drug interaction") == 43
    assert top.items[0] == ("Acute kidney injury", 80)

def test_drug_ineffective_differs_between_all_cases_and_serious_cases(facts):
    assert facts.get("cases.top_reactions").value.count_for("Drug ineffective") == 54
    assert facts.get("cases.top_serious_reactions").value.count_for("Drug ineffective") == 53

def test_a_case_reporting_a_term_twice_is_counted_once(dataset):
    repeated = [c for c in dataset.cases if len(c.pts) != len(set(c.pts))]
    for case in repeated:
        assert len(case.distinct_pts) < len(case.pts)

def test_e2b_unit_800_is_decades_not_years():
    assert age_in_years(7, "800.0") == 70.0
    assert age_in_years(9, 800) == 90.0
    assert age_in_years(3, "800") == 30.0

def test_age_units_convert_to_years():
    assert age_in_years(65, "year") == 65.0
    assert age_in_years(24, "month") == 2.0
    assert age_in_years(None, "year") is None
    assert age_in_years(42, None) == 42.0

def test_age_groups_cover_every_case(facts):
    groups: Distribution = facts.get("cases.by_age_group").value
    assert sum(groups.counts) == 1024
    assert groups.count_for("Unknown") == 83
    assert groups.count_for("Elderly (>=65 years)") == 676

def test_seriousness_criteria_are_not_mutually_exclusive(facts):
    criteria: Distribution = facts.get("cases.by_seriousness_criterion").value
    assert criteria.total == 1023
    assert sum(criteria.counts) > 1023

def test_monthly_volume_sums_to_the_case_total(facts):
    months: Distribution = facts.get("cases.by_month").value
    assert sum(months.counts) == 1024
    assert months.labels[0] == "2024-12"
    assert months.labels[-1] == "2025-12"
    assert len(months.labels) == 13

def test_period_halves_partition_the_cases(facts):
    first = facts.get("cases.top_reactions_first_half").value
    second = facts.get("cases.top_reactions_second_half").value
    assert first.total + second.total == 1024

def test_outcomes_are_counted_per_reaction_not_per_case(facts):
    outcomes: Distribution = facts.get("events.by_outcome").value
    assert sum(outcomes.counts) == facts.get("events.total").value == 3423
