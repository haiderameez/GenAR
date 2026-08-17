from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError
from .facts import GRAIN_NONE, Fact
from .loader import AGE_GROUP_UNKNOWN, Case, Dataset, normalise_age_unit


NON_COUNTRY_CODES = {"eu", "eea", "ec", "european union"}

SAMPLE_SIZE = 5


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    summary: str
    count: int
    detail: str = ""
    sample_case_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "summary": self.summary,
            "count": self.count,
            "detail": self.detail,
            "sample_case_ids": list(self.sample_case_ids),
        }

@dataclass
class DataQualityReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    def raise_if_invalid(self) -> None:
        if self.errors:
            lines = "\n".join(f"  - {f.summary} ({f.count})" for f in self.errors)
            raise ValidationError(f"dataset failed validation:\n{lines}")

    def to_dict(self) -> dict[str, Any]:
        return {"findings": [f.to_dict() for f in self.findings]}

    def to_markdown(self) -> str:
        rows = ["| Check | Severity | Cases | Note |", "| --- | --- | --- | --- |"]
        for f in self.findings:
            note = f.detail or f.summary
            rows.append(f"| {f.summary} | {f.severity} | {f.count} | {note} |")
        return "\n".join(rows)

def _sample(cases: list[Case]) -> tuple[str, ...]:
    return tuple(case.case_id for case in cases[:SAMPLE_SIZE])

def quality_fact(report: DataQualityReport) -> Fact:
    return Fact(
        id="meta.data_quality",
        label="Data-quality checks performed before analysis",
        value=[finding.to_dict() for finding in report.findings],
        grain=GRAIN_NONE,
        method="checks run by validate.py against the source file before any analysis",
    )

def provenance_fact(dataset: Dataset) -> Fact:
    return Fact(
        id="meta.dataset_provenance",
        label="Source dataset",
        value={
            "path": dataset.source_path,
            "sha256": dataset.source_sha256,
            "rows": len(dataset.all_rows),
            "cases": len(dataset.cases),
        },
        grain=GRAIN_NONE,
        method="SHA-256 of the source file, with row and case counts as loaded",
    )

def validate(dataset: Dataset) -> DataQualityReport:
    report = DataQualityReport()
    cases = list(dataset.cases)
    rows = list(dataset.all_rows)

    report.add(
        Finding(
            id="structure.rows_vs_cases",
            severity="info",
            summary="Rows resolved to cases",
            count=len(cases),
            detail=(
                f"{len(rows)} rows in the source file resolved to {len(cases)} cases; "
                f"{dataset.superseded_row_count} rows were superseded follow-up versions "
                "of a case already present and were excluded from case-level counts."
            ),
        )
    )

    version_counts = Counter(case.case_id for case in rows)
    multi_version = [case_id for case_id, n in version_counts.items() if n > 1]
    if multi_version:
        report.add(
            Finding(
                id="structure.multi_version_cases",
                severity="info",
                summary="Cases supplied at more than one version",
                count=len(multi_version),
                detail=(
                    "Highest safetyreportversion retained per case. Superseded versions "
                    "differ in reaction list, receipt date and company number."
                ),
                sample_case_ids=tuple(multi_version[:SAMPLE_SIZE]),
            )
        )

    misaligned = [case for case in rows if not case.reactions_aligned]
    report.add(
        Finding(
            id="reactions.alignment",
            severity="error" if misaligned else "info",
            summary="Reaction, outcome and MedDRA-version lists align on every row",
            count=len(misaligned),
            detail=(
                "Reaction outcomes are paired positionally with reaction terms, so this "
                "invariant must hold for outcome attribution to be correct."
                if not misaligned
                else "Positional outcome pairing is unsafe on the listed rows."
            ),
            sample_case_ids=_sample(misaligned),
        )
    )

    comma_terms = sorted(
        {reaction.pt for case in rows for reaction in case.reactions if ", " in reaction.pt}
    )
    if comma_terms:
        report.add(
            Finding(
                id="reactions.comma_bearing_terms",
                severity="info",
                summary="MedDRA terms containing a comma, preserved by the split rule",
                count=len(comma_terms),
                detail=(
                    "Terms recovered rather than split into phantom reactions: "
                    + "; ".join(comma_terms)
                ),
            )
        )

    no_outcome = [case for case in cases if any(r.outcome is None for r in case.reactions)]
    if no_outcome:
        report.add(
            Finding(
                id="reactions.missing_outcome",
                severity="warning",
                summary="Cases with at least one reaction carrying no outcome",
                count=len(no_outcome),
                sample_case_ids=_sample(no_outcome),
            )
        )

    decade_coded = [
        case
        for case in cases
        if normalise_age_unit(case.raw.get("patient_patientonsetageunit")) == "decade"
    ]
    if decade_coded:
        report.add(
            Finding(
                id="age.decade_unit",
                severity="warning",
                summary="Ages supplied with E2B unit code 800 (decade)",
                count=len(decade_coded),
                detail=(
                    "Converted to years by multiplying by 10. Reading code 800 as 'years' "
                    "would understate these patients' ages by a factor of ten."
                ),
                sample_case_ids=_sample(decade_coded),
            )
        )

    sub_year = [
        case
        for case in cases
        if normalise_age_unit(case.raw.get("patient_patientonsetageunit"))
        in {"month", "week", "day", "hour"}
    ]
    if sub_year:
        report.add(
            Finding(
                id="age.sub_year_units",
                severity="info",
                summary="Ages supplied in months, weeks or days",
                count=len(sub_year),
                detail="Converted to fractional years before age-group assignment.",
                sample_case_ids=_sample(sub_year),
            )
        )

    unitless = [
        case
        for case in cases
        if case.age_years is not None
        and not normalise_age_unit(case.raw.get("patient_patientonsetageunit"))
    ]
    if unitless:
        report.add(
            Finding(
                id="age.missing_unit",
                severity="warning",
                summary="Ages supplied without a unit, assumed to be years",
                count=len(unitless),
                sample_case_ids=_sample(unitless),
            )
        )

    unknown_age = [case for case in cases if case.age_group == AGE_GROUP_UNKNOWN]
    report.add(
        Finding(
            id="age.unknown",
            severity="info",
            summary="Cases with no usable age",
            count=len(unknown_age),
            detail="Reported as an explicit 'Unknown' stratum rather than dropped from the denominator.",
            sample_case_ids=_sample(unknown_age),
        )
    )

    supplied_age_group = [case for case in cases if str(case.raw.get("patient_patientagegroup") or "").strip()]
    report.add(
        Finding(
            id="age.supplied_group_unused",
            severity="info",
            summary="Cases carrying the coarse patient_patientagegroup field",
            count=len(supplied_age_group),
            detail=(
                f"Populated on {len(supplied_age_group)} of {len(cases)} cases, so age strata are "
                "derived from patient_patientonsetage instead. The supplied field is not used."
            ),
        )
    )

    no_sex = [case for case in cases if not case.sex]
    report.add(
        Finding(
            id="sex.missing",
            severity="info",
            summary="Cases with no recorded sex",
            count=len(no_sex),
            detail="Reported as an explicit 'Unknown' stratum.",
            sample_case_ids=_sample(no_sex),
        )
    )

    region_coded = [case for case in cases if case.country in NON_COUNTRY_CODES]
    if region_coded:
        report.add(
            Finding(
                id="country.region_code",
                severity="warning",
                summary="Cases whose occurcountry is a region, not a country",
                count=len(region_coded),
                detail=(
                    "Reported under their supplied label rather than reassigned to a member "
                    "state, which the data does not support. Country-level totals are "
                    "therefore incomplete for these cases."
                ),
                sample_case_ids=_sample(region_coded),
            )
        )

    country_disagreement = [
        case
        for case in cases
        if case.reporter_country and case.country and case.reporter_country != case.country
    ]
    if country_disagreement:
        report.add(
            Finding(
                id="country.source_disagreement",
                severity="info",
                summary="Cases where occurcountry and primarysource_reportercountry differ",
                count=len(country_disagreement),
                detail="Geographic analysis uses occurcountry throughout.",
                sample_case_ids=_sample(country_disagreement),
            )
        )

    flagged = [case for case in cases if case.duplicate_flagged]
    if flagged:
        report.add(
            Finding(
                id="provenance.duplicate_flag",
                severity="warning",
                summary="Cases carrying a source duplicate flag",
                count=len(flagged),
                detail=(
                    "Retained in all counts. Whether a flagged report is a true duplicate is "
                    "a medical-review decision, not one this pipeline should make silently."
                ),
                sample_case_ids=_sample(flagged),
            )
        )

    report_types = Counter(case.report_type or "unspecified" for case in cases)
    report.add(
        Finding(
            id="provenance.report_type",
            severity="info",
            summary="Report-type mix",
            count=len(cases),
            detail=", ".join(f"{name}: {n}" for name, n in report_types.most_common()),
        )
    )

    for check_id, summary in (
        ("absent.system_organ_class", "No MedDRA System Organ Class field is supplied"),
        ("absent.product_label", "No product label or CCDS is supplied"),
        ("absent.history_of_actions", "No history-of-actions data is supplied"),
        ("absent.prior_period", "No prior reporting period is supplied"),
    ):
        report.add(
            Finding(
                id=check_id,
                severity="info",
                summary=summary,
                count=0,
                detail="Reported as unavailable in the output; never inferred and never rendered as zero.",
            )
        )

    return report
