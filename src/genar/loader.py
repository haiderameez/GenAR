from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .errors import DatasetError, DatasetNotFoundError


E2B_AGE_UNITS = {
    "800": "decade",
    "801": "year",
    "802": "month",
    "803": "week",
    "804": "day",
    "805": "hour",
}

_UNIT_TO_YEARS = {
    "decade": 10.0,
    "year": 1.0,
    "month": 1.0 / 12.0,
    "week": 1.0 / 52.1775,
    "day": 1.0 / 365.25,
    "hour": 1.0 / 8766.0,
}


AGE_GROUPS: tuple[tuple[str, float, float], ...] = (
    ("Neonate (<28 days)", 0.0, 28.0 / 365.25),
    ("Infant (28 days to <2 years)", 28.0 / 365.25, 2.0),
    ("Child (2 to <12 years)", 2.0, 12.0),
    ("Adolescent (12 to <18 years)", 12.0, 18.0),
    ("Adult (18 to <65 years)", 18.0, 65.0),
    ("Elderly (>=65 years)", 65.0, float("inf")),
)
AGE_GROUP_UNKNOWN = "Unknown"
AGE_GROUP_ORDER: tuple[str, ...] = tuple(name for name, _, _ in AGE_GROUPS) + (AGE_GROUP_UNKNOWN,)


def age_group_for(years: float | None) -> str:
    if years is None or years < 0:
        return AGE_GROUP_UNKNOWN
    for name, low, high in AGE_GROUPS:
        if low <= years < high:
            return name
    return AGE_GROUP_UNKNOWN

_WHOLE_FLOAT_TEXT = re.compile(r"^(\d+)\.0+$")

def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    match = _WHOLE_FLOAT_TEXT.match(text)
    return match.group(1) if match else text

def _number(value: Any) -> float | None:
    text = _text(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None

def parse_e2b_date(value: Any) -> date | None:
    text = _text(value)
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None

def normalise_age_unit(value: Any) -> str | None:
    text = _text(value).lower()
    if not text:
        return None
    code = text.split(".")[0]
    if code in E2B_AGE_UNITS:
        return E2B_AGE_UNITS[code]
    return text if text in _UNIT_TO_YEARS else None

def age_in_years(raw_age: Any, raw_unit: Any) -> float | None:
    value = _number(raw_age)
    if value is None or value < 0:
        return None
    unit = normalise_age_unit(raw_unit)
    if unit is None:

        unit = "year"
    return value * _UNIT_TO_YEARS[unit]

def _yes(value: Any) -> bool:
    return _text(value).lower() == "yes"

def _comma_tokens(cell: Any) -> list[str]:
    return [part.strip() for part in _text(cell).split(",") if part.strip()]

def split_meddra_terms(cell: Any) -> list[str]:
    terms: list[str] = []
    for token in _comma_tokens(cell):
        if terms and token[:1].islower():
            terms[-1] = f"{terms[-1]}, {token}"
        else:
            terms.append(token)
    return terms

@dataclass(frozen=True)
class Reaction:

    pt: str
    outcome: str | None
    meddra_version: str | None

def build_reactions(row: dict[str, Any]) -> tuple[tuple[Reaction, ...], bool]:
    terms = split_meddra_terms(row.get("patient_reaction_reactionmeddrapt"))
    outcomes = _comma_tokens(row.get("patient_reaction_reactionoutcome"))
    versions = _comma_tokens(row.get("patient_reaction_reactionmeddraversionpt"))

    aligned = len(terms) == len(outcomes) == len(versions)
    reactions = tuple(
        Reaction(
            pt=term,
            outcome=outcomes[index] if index < len(outcomes) else None,
            meddra_version=versions[index] if index < len(versions) else None,
        )
        for index, term in enumerate(terms)
    )
    return reactions, aligned

SERIOUSNESS_CRITERIA: dict[str, str] = {
    "seriousnessdeath": "Death",
    "seriousnesslifethreatening": "Life-threatening",
    "seriousnesshospitalization": "Hospitalisation (initial or prolonged)",
    "seriousnessdisabling": "Persistent or significant disability",
    "seriousnesscongenitalanomali": "Congenital anomaly",
    "seriousnessother": "Other medically important condition",
}


@dataclass(frozen=True)
class Case:

    case_id: str
    version: float
    receive_date: date | None
    receipt_date: date | None
    country: str
    reporter_country: str
    sex: str
    age_years: float | None
    age_group: str
    serious: bool
    seriousness_criteria: dict[str, bool]
    expedited: bool
    report_type: str
    reporter_qualification: str
    reactions: tuple[Reaction, ...]
    duplicate_flagged: bool
    reactions_aligned: bool
    raw: dict[str, Any] = field(repr=False, compare=False)

    @property
    def pts(self) -> tuple[str, ...]:
        return tuple(reaction.pt for reaction in self.reactions)

    @property
    def distinct_pts(self) -> frozenset[str]:
        return frozenset(self.pts)

def build_case(row: dict[str, Any]) -> Case:
    reactions, aligned = build_reactions(row)
    years = age_in_years(
        row.get("patient_patientonsetage"), row.get("patient_patientonsetageunit")
    )
    return Case(
        case_id=_text(row.get("safetyreportid")),
        version=_number(row.get("safetyreportversion")) or 0.0,
        receive_date=parse_e2b_date(row.get("receivedate")),
        receipt_date=parse_e2b_date(row.get("receiptdate")),

        country=_text(row.get("occurcountry")).lower(),
        reporter_country=_text(row.get("primarysource_reportercountry")).lower(),
        sex=_text(row.get("patient_patientsex")).lower(),
        age_years=years,
        age_group=age_group_for(years),
        serious=_text(row.get("serious")).lower() == "serious",
        seriousness_criteria={
            label: _yes(row.get(column)) for column, label in SERIOUSNESS_CRITERIA.items()
        },
        expedited=_yes(row.get("fulfillexpeditecriteria")),
        report_type=_text(row.get("reporttype")).lower(),
        reporter_qualification=_text(row.get("primarysource_qualification")).lower(),
        reactions=reactions,
        duplicate_flagged=bool(_number(row.get("duplicate"))),
        reactions_aligned=aligned,
        raw=row,
    )

@dataclass(frozen=True)
class Dataset:

    cases: tuple[Case, ...]
    all_rows: tuple[Case, ...]
    source_path: str
    source_sha256: str

    @property
    def superseded_row_count(self) -> int:
        return len(self.all_rows) - len(self.cases)

    def reaction_count(self, *, deduplicated: bool = True) -> int:
        rows = self.cases if deduplicated else self.all_rows
        return sum(len(case.reactions) for case in rows)

    @property
    def period_start(self) -> date | None:
        dates = [case.receive_date for case in self.cases if case.receive_date]
        return min(dates) if dates else None

    @property
    def period_end(self) -> date | None:
        dates = [case.receive_date for case in self.cases if case.receive_date]
        return max(dates) if dates else None

def deduplicate_to_latest(rows: list[Case]) -> list[Case]:
    latest: dict[str, Case] = {}
    for case in rows:
        seen = latest.get(case.case_id)
        if seen is None or case.version > seen.version:
            latest[case.case_id] = case
    return list(latest.values())

def _read_xlsx(path: Path) -> Iterator[dict[str, Any]]:
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        header = [_text(cell) for cell in next(rows)]
        for values in rows:
            if all(value is None for value in values):
                continue
            yield dict(zip(header, values))
    finally:
        workbook.close()

def _read_csv(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        yield from csv.DictReader(handle)

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def load_dataset(path: str | Path) -> Dataset:
    path = Path(path)
    if not path.exists():
        raise DatasetNotFoundError(f"dataset not found: {path}")

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        raw_rows = _read_xlsx(path)
    elif suffix in {".csv", ".txt"}:
        raw_rows = _read_csv(path)
    else:
        raise DatasetError(f"unsupported dataset format: {path.suffix}")

    all_rows = [build_case(row) for row in raw_rows]
    cases = deduplicate_to_latest(all_rows)
    cases.sort(key=lambda case: (case.receive_date or date.min, case.case_id))

    return Dataset(
        cases=tuple(cases),
        all_rows=tuple(all_rows),
        source_path=str(path),
        source_sha256=_sha256(path),
    )
