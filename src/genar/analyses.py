from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any, Callable, Iterable, Sequence

from .facts import (
    GRAIN_CASE,
    GRAIN_EVENT,
    GRAIN_NONE,
    SCOPE_ALL_ROWS,
    SCOPE_DEDUPLICATED,
    STATUS_NOT_AVAILABLE,
    STATUS_NOT_PROVIDED,
    Distribution,
    Fact,
    FactStore,
)
from .errors import ConfigurationError, EvidenceNotFoundError
from .loader import AGE_GROUP_ORDER, Case, Dataset

AnalysisFn = Callable[[Dataset], Fact | list[Fact]]

_PRODUCER: dict[str, AnalysisFn] = {}


TOP_N = 10


def analysis(*fact_ids: str) -> Callable[[AnalysisFn], AnalysisFn]:

    def decorate(fn: AnalysisFn) -> AnalysisFn:
        for fact_id in fact_ids:
            if fact_id in _PRODUCER:
                raise ConfigurationError(f"{fact_id} already has a producer")
            _PRODUCER[fact_id] = fn
        return fn

    return decorate

def registered_fact_ids() -> tuple[str, ...]:
    return tuple(sorted(_PRODUCER))

def compute(dataset: Dataset, fact_ids: Iterable[str] | None = None) -> FactStore:
    wanted = sorted(set(fact_ids)) if fact_ids is not None else sorted(_PRODUCER)
    unknown = [fact_id for fact_id in wanted if fact_id not in _PRODUCER]
    if unknown:
        raise EvidenceNotFoundError("no analysis produces: " + ", ".join(unknown))

    store = FactStore()
    for fn in dict.fromkeys(_PRODUCER[fact_id] for fact_id in wanted):
        produced = fn(dataset)
        for fact in produced if isinstance(produced, list) else [produced]:
            store.add(fact)
    return store

def _ids(cases: Iterable[Case]) -> tuple[str, ...]:
    return tuple(case.case_id for case in cases)

def _distribution_by(
    cases: Sequence[Case],
    key: Callable[[Case], str],
    *,
    order: tuple[str, ...] | None = None,
) -> Distribution:
    counts: Counter[str] = Counter()
    support: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        label = key(case)
        counts[label] += 1
        support[label].append(case.case_id)
    return Distribution.from_counts(counts, total=len(cases), support=support, order=order)

def _case_counts_by_pt(cases: Sequence[Case]) -> Distribution:
    counts: Counter[str] = Counter()
    support: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        for pt in case.distinct_pts:
            counts[pt] += 1
            support[pt].append(case.case_id)
    return Distribution.from_counts(counts, total=len(cases), support=support)

def _month_label(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"

def _months_in_period(dataset: Dataset) -> tuple[str, ...]:
    start, end = dataset.period_start, dataset.period_end
    if not start or not end:
        return ()
    labels: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        labels.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(labels)

@analysis("period.start", "period.end", "period.label", "period.days")
def period(dataset: Dataset) -> list[Fact]:
    start, end = dataset.period_start, dataset.period_end
    method = "min/max of receivedate across cases"
    if not start or not end:
        return [
            Fact(id=fact_id, label=label, value=None, grain=GRAIN_NONE, method=method,
                 status=STATUS_NOT_AVAILABLE)
            for fact_id, label in (
                ("period.start", "Reporting period start"),
                ("period.end", "Reporting period end"),
                ("period.label", "Reporting period"),
                ("period.days", "Reporting period length in days"),
            )
        ]
    return [
        Fact(id="period.start", label="Reporting period start", value=start.isoformat(),
             grain=GRAIN_NONE, method=method),
        Fact(id="period.end", label="Reporting period end", value=end.isoformat(),
             grain=GRAIN_NONE, method=method),
        Fact(id="period.label", label="Reporting period",
             value=f"{start.isoformat()} to {end.isoformat()}", grain=GRAIN_NONE, method=method),
        Fact(id="period.days", label="Reporting period length in days",
             value=(end - start).days + 1, grain=GRAIN_NONE,
             method="inclusive day count between first and last receivedate"),
    ]

@analysis("cases.total", "cases.serious", "cases.non_serious")
def case_volume(dataset: Dataset) -> list[Fact]:
    cases = dataset.cases
    serious = [case for case in cases if case.serious]
    non_serious = [case for case in cases if not case.serious]
    return [
        Fact(id="cases.total", label="Total cases", value=len(cases), grain=GRAIN_CASE,
             unit="cases", case_ids=_ids(cases),
             method="count of distinct safetyreportid, highest version retained"),
        Fact(id="cases.serious", label="Serious cases", value=len(serious), grain=GRAIN_CASE,
             unit="cases", case_ids=_ids(serious), denominator_id="cases.total",
             method="count of cases where serious == 'serious'"),
        Fact(id="cases.non_serious", label="Non-serious cases", value=len(non_serious),
             grain=GRAIN_CASE, unit="cases", case_ids=_ids(non_serious),
             denominator_id="cases.total",
             method="count of cases where serious != 'serious'"),
    ]

@analysis("cases.by_seriousness_criterion", "cases.fatal")
def seriousness_criteria(dataset: Dataset) -> list[Fact]:
    serious = [case for case in dataset.cases if case.serious]
    counts: Counter[str] = Counter()
    support: dict[str, list[str]] = defaultdict(list)
    for case in serious:
        for label, met in case.seriousness_criteria.items():
            if met:
                counts[label] += 1
                support[label].append(case.case_id)
    fatal = [case for case in dataset.cases if case.seriousness_criteria.get("Death")]
    return [
        Fact(
            id="cases.by_seriousness_criterion",
            label="Serious cases by regulatory seriousness criterion (criteria are not mutually exclusive)",
            value=Distribution.from_counts(counts, total=len(serious), support=support),
            grain=GRAIN_CASE, unit="cases", case_ids=_ids(serious),
            method="count of serious cases carrying each seriousness flag; one case may meet several",
        ),
        Fact(id="cases.fatal", label="Cases reporting a fatal outcome", value=len(fatal),
             grain=GRAIN_CASE, unit="cases", case_ids=_ids(fatal),
             denominator_id="cases.total",
             method="count of cases where seriousnessdeath == 'yes'"),
    ]

@analysis("cases.expedited")
def expedited(dataset: Dataset) -> Fact:
    cases = [case for case in dataset.cases if case.expedited]
    return Fact(
        id="cases.expedited",
        label="Cases meeting expedited (15-day Alert) reporting criteria",
        value=len(cases), grain=GRAIN_CASE, unit="cases", case_ids=_ids(cases),
        denominator_id="cases.total",
        method="count of cases where fulfillexpeditecriteria == 'yes'",
    )

@analysis("cases.duplicate_flagged")
def duplicate_flagged(dataset: Dataset) -> Fact:
    cases = [case for case in dataset.cases if case.duplicate_flagged]
    return Fact(
        id="cases.duplicate_flagged",
        label="Cases carrying a source duplicate flag (retained in all counts)",
        value=len(cases), grain=GRAIN_CASE, unit="cases", case_ids=_ids(cases),
        denominator_id="cases.total",
        method="count of cases where the source duplicate field is set",
    )

@analysis("cases.by_age_group")
def by_age_group(dataset: Dataset) -> Fact:
    return Fact(
        id="cases.by_age_group", label="Cases by age group",
        value=_distribution_by(dataset.cases, lambda c: c.age_group, order=AGE_GROUP_ORDER),
        grain=GRAIN_CASE, unit="cases", case_ids=_ids(dataset.cases),
        method="ICH E2B age strata derived from patient_patientonsetage normalised to years",
    )

@analysis("cases.by_sex")
def by_sex(dataset: Dataset) -> Fact:
    return Fact(
        id="cases.by_sex", label="Cases by sex",
        value=_distribution_by(dataset.cases, lambda c: c.sex or "unknown"),
        grain=GRAIN_CASE, unit="cases", case_ids=_ids(dataset.cases),
        method="count of cases by patient_patientsex; blank recorded as 'unknown'",
    )

@analysis("cases.by_country")
def by_country(dataset: Dataset) -> Fact:
    return Fact(
        id="cases.by_country", label="Cases by country or region of occurrence",
        value=_distribution_by(dataset.cases, lambda c: c.country or "unknown"),
        grain=GRAIN_CASE, unit="cases", case_ids=_ids(dataset.cases),
        method=(
            "count of cases by occurcountry as supplied; 'eu' is a region rather than a "
            "country and is reported under its own label"
        ),
    )

@analysis("cases.by_reporter_qualification")
def by_reporter(dataset: Dataset) -> Fact:
    return Fact(
        id="cases.by_reporter_qualification", label="Cases by reporter qualification",
        value=_distribution_by(dataset.cases, lambda c: c.reporter_qualification or "unknown"),
        grain=GRAIN_CASE, unit="cases", case_ids=_ids(dataset.cases),
        method="count of cases by primarysource_qualification",
    )

@analysis("cases.by_report_type")
def by_report_type(dataset: Dataset) -> Fact:
    return Fact(
        id="cases.by_report_type", label="Cases by report type",
        value=_distribution_by(dataset.cases, lambda c: c.report_type or "unspecified"),
        grain=GRAIN_CASE, unit="cases", case_ids=_ids(dataset.cases),
        method="count of cases by reporttype",
    )

@analysis("cases.top_reactions", "cases.top_serious_reactions")
def top_reactions(dataset: Dataset) -> list[Fact]:
    all_cases = _case_counts_by_pt(dataset.cases)
    serious_only = _case_counts_by_pt([case for case in dataset.cases if case.serious])
    return [
        Fact(
            id="cases.top_reactions",
            label=f"Most frequently reported reactions, by number of cases (top {TOP_N})",
            value=all_cases.top(TOP_N), grain=GRAIN_CASE, unit="cases",
            case_ids=_ids(dataset.cases),
            method="count of distinct cases reporting each MedDRA preferred term",
        ),
        Fact(
            id="cases.top_serious_reactions",
            label=f"Most frequently reported reactions among serious cases (top {TOP_N})",
            value=serious_only.top(TOP_N), grain=GRAIN_CASE, unit="cases",
            case_ids=_ids([case for case in dataset.cases if case.serious]),
            method="count of distinct serious cases reporting each MedDRA preferred term",
        ),
    ]

@analysis("events.total", "events.total_all_rows", "events.distinct_terms")
def event_volume(dataset: Dataset) -> list[Fact]:
    terms = {reaction.pt for case in dataset.cases for reaction in case.reactions}
    return [
        Fact(id="events.total", label="Reaction events reported", grain=GRAIN_EVENT,
             scope=SCOPE_DEDUPLICATED, unit="reaction events",
             value=dataset.reaction_count(deduplicated=True), case_ids=_ids(dataset.cases),
             method="sum of reaction terms across cases, latest version of each case only"),
        Fact(id="events.total_all_rows",
             label="Reaction events across all source rows, superseded case versions included",
             grain=GRAIN_EVENT, scope=SCOPE_ALL_ROWS, unit="reaction events",
             value=dataset.reaction_count(deduplicated=False), case_ids=_ids(dataset.all_rows),
             method="sum of reaction terms across every row in the source file"),
        Fact(id="events.distinct_terms", label="Distinct MedDRA preferred terms reported",
             value=len(terms), grain=GRAIN_EVENT, scope=SCOPE_DEDUPLICATED,
             unit="preferred terms", case_ids=_ids(dataset.cases),
             method="count of distinct MedDRA preferred terms across cases"),
    ]

@analysis("events.by_outcome")
def by_outcome(dataset: Dataset) -> Fact:
    counts: Counter[str] = Counter()
    support: dict[str, list[str]] = defaultdict(list)
    total = 0
    for case in dataset.cases:
        for reaction in case.reactions:
            label = reaction.outcome or "unknown"
            counts[label] += 1
            total += 1
            support[label].append(case.case_id)
    return Fact(
        id="events.by_outcome", label="Reaction events by reported outcome",
        value=Distribution.from_counts(counts, total=total, support=support),
        grain=GRAIN_EVENT, scope=SCOPE_DEDUPLICATED, unit="reaction events",
        case_ids=_ids(dataset.cases),
        method="count of reaction events by reactionoutcome, paired positionally with each reaction term",
    )

@analysis("cases.by_month", "cases.serious_by_month")
def by_month(dataset: Dataset) -> list[Fact]:
    months = _months_in_period(dataset)
    dated = [case for case in dataset.cases if case.receive_date]
    return [
        Fact(id="cases.by_month", label="Cases received by month",
             value=_distribution_by(dated, lambda c: _month_label(c.receive_date), order=months),
             grain=GRAIN_CASE, unit="cases", case_ids=_ids(dated),
             method="count of cases by calendar month of receivedate"),
        Fact(id="cases.serious_by_month", label="Serious cases received by month",
             value=_distribution_by([c for c in dated if c.serious],
                                    lambda c: _month_label(c.receive_date), order=months),
             grain=GRAIN_CASE, unit="cases",
             case_ids=_ids([c for c in dated if c.serious]),
             method="count of serious cases by calendar month of receivedate"),
    ]

@analysis("cases.top_reactions_first_half", "cases.top_reactions_second_half")
def reactions_by_half(dataset: Dataset) -> list[Fact]:
    dated = [case for case in dataset.cases if case.receive_date]
    start, end = dataset.period_start, dataset.period_end
    if not dated or not start or not end:
        midpoint = None
    else:
        midpoint = start + (end - start) / 2

    leading = _case_counts_by_pt(dataset.cases).top(TOP_N).labels
    first = [case for case in dated if midpoint and case.receive_date <= midpoint]
    second = [case for case in dated if midpoint and case.receive_date > midpoint]

    def counts_for(cases: Sequence[Case]) -> Distribution:
        counts: Counter[str] = Counter()
        support: dict[str, list[str]] = defaultdict(list)
        for case in cases:
            for pt in case.distinct_pts & set(leading):
                counts[pt] += 1
                support[pt].append(case.case_id)
        return Distribution.from_counts(
            {term: counts.get(term, 0) for term in leading},
            total=len(cases), support=support, order=leading,
        )

    half_note = (
        f"cases received on or before {midpoint.isoformat()}" if midpoint else "unavailable"
    )
    return [
        Fact(id="cases.top_reactions_first_half",
             label="Leading reactions in the first half of the reporting period",
             value=counts_for(first), grain=GRAIN_CASE, unit="cases", case_ids=_ids(first),
             method=f"count of distinct cases reporting each leading term among {half_note}"),
        Fact(id="cases.top_reactions_second_half",
             label="Leading reactions in the second half of the reporting period",
             value=counts_for(second), grain=GRAIN_CASE, unit="cases", case_ids=_ids(second),
             method="count of distinct cases reporting each leading term in the remainder of the period"),
    ]

@analysis("cases.listing")
def case_listing(dataset: Dataset) -> Fact:
    rows: list[dict[str, Any]] = [
        {
            "case_id": case.case_id,
            "received": case.receive_date.isoformat() if case.receive_date else "unknown",
            "country": case.country or "unknown",
            "sex": case.sex or "unknown",
            "age_group": case.age_group,
            "serious": "serious" if case.serious else "not serious",
            "expedited": "yes" if case.expedited else "no",
            "reactions": "; ".join(case.pts),
            "outcomes": "; ".join(r.outcome or "unknown" for r in case.reactions),
        }
        for case in dataset.cases
    ]
    return Fact(
        id="cases.listing", label="Case index", value=rows, grain=GRAIN_CASE, unit="cases",
        case_ids=_ids(dataset.cases),
        method="one row per case, ordered by receivedate then case identifier",
    )

@analysis("absent.system_organ_class")
def absent_soc(dataset: Dataset) -> Fact:
    return Fact(
        id="absent.system_organ_class",
        label="Analysis of reactions by MedDRA System Organ Class",
        value=None, grain=GRAIN_NONE, status=STATUS_NOT_AVAILABLE,
        method=(
            "the dataset carries patient_reaction_reactionmeddrapt only; no System Organ Class "
            "field and no MedDRA hierarchy were supplied, so terms are reported at preferred-term level"
        ),
    )

@analysis("absent.expectedness")
def absent_expectedness(dataset: Dataset) -> Fact:
    return Fact(
        id="absent.expectedness",
        label="Classification of reactions as labelled or unlabelled",
        value=None, grain=GRAIN_NONE, status=STATUS_NOT_AVAILABLE,
        method="determining expectedness requires the approved product label or CCDS, which was not supplied",
    )

@analysis("absent.history_of_actions")
def absent_actions(dataset: Dataset) -> Fact:
    return Fact(
        id="absent.history_of_actions",
        label="Actions taken for safety reasons during the reporting period",
        value=None, grain=GRAIN_NONE, status=STATUS_NOT_PROVIDED,
        method="no labelling changes, regulatory communications or safety-related studies were supplied with this dataset",
    )

@analysis("absent.cumulative_prior_period")
def absent_cumulative(dataset: Dataset) -> Fact:
    return Fact(
        id="absent.cumulative_prior_period",
        label="Cumulative case counts from previous reporting periods",
        value=None, grain=GRAIN_NONE, status=STATUS_NOT_PROVIDED,
        method=(
            "only the current reporting interval was supplied; cumulative figures are reported "
            "as unavailable rather than as zero, which would assert that no earlier cases exist"
        ),
    )
