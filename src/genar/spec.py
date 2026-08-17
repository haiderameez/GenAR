from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .errors import ConfigurationError, EvidenceNotFoundError
from .facts import GRAIN_NONE, Fact


CLAIM_OBSERVED = "observed"
CLAIM_DERIVED = "derived"
CLAIM_INTERPRETATION = "interpretation"

CLAIM_LEVEL_RULES: dict[str, str] = {
    CLAIM_OBSERVED: (
        "State only the figures supplied. Do not rank, compare, aggregate or "
        "characterise them."
    ),
    CLAIM_DERIVED: (
        "You may state comparisons, rankings and changes that are directly readable "
        "from the supplied figures -- which term is most frequent, that a count rose "
        "or fell between two supplied periods. Present these as observations of the "
        "reported data. You may not explain them, attribute a cause, judge their "
        "clinical significance, or describe the product's safety profile."
    ),
    CLAIM_INTERPRETATION: (
        "You may assess the significance of the supplied figures. This level is not "
        "granted to any section in this report type."
    ),
}

GENERATOR_LLM = "llm"
GENERATOR_DETERMINISTIC = "deterministic"
GENERATORS = frozenset({GENERATOR_LLM, GENERATOR_DETERMINISTIC})


@dataclass(frozen=True)
class SectionSpec:

    id: str
    heading: str
    requires: tuple[str, ...]
    generator: str = GENERATOR_LLM
    claim_level: str = CLAIM_DERIVED
    instructions: str = ""
    tables: tuple[str, ...] = ()
    renderer: str | None = None
    max_words: int | None = None
    closing_note: str = ""

    @property
    def uses_llm(self) -> bool:
        return self.generator == GENERATOR_LLM

    @property
    def claim_rule(self) -> str:
        return CLAIM_LEVEL_RULES[self.claim_level]

@dataclass(frozen=True)
class ReportSpec:
    report_type: str
    title: str
    product: str
    sections: tuple[SectionSpec, ...]
    regulatory_basis: str = ""
    source_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def section(self, section_id: str) -> SectionSpec:
        for section in self.sections:
            if section.id == section_id:
                return section
        raise EvidenceNotFoundError(f"{self.report_type}: no section {section_id!r}")

    @property
    def required_fact_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for section in self.sections:
            for fact_id in (*section.requires, *section.tables):
                seen.setdefault(fact_id, None)
        return tuple(seen)

    @property
    def llm_section_count(self) -> int:
        return sum(1 for section in self.sections if section.uses_llm)

def _as_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise ConfigurationError(f"{where}: expected a list, got a string")
    return tuple(str(item) for item in value)

def load_spec(path: str | Path) -> ReportSpec:
    path = Path(path)
    if not path.exists():
        raise ConfigurationError(f"report configuration not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for key in ("report_type", "title", "product", "sections"):
        if not raw.get(key):
            raise ConfigurationError(f"{path.name}: missing required key {key!r}")

    sections: list[SectionSpec] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw["sections"]):
        where = f"{path.name}: section {index + 1}"
        section_id = item.get("id")
        if not section_id:
            raise ConfigurationError(f"{where}: missing 'id'")
        if section_id in seen_ids:
            raise ConfigurationError(f"{path.name}: duplicate section id {section_id!r}")
        seen_ids.add(section_id)

        generator = item.get("generator", GENERATOR_LLM)
        if generator not in GENERATORS:
            raise ConfigurationError(f"{where}: generator must be one of {sorted(GENERATORS)}")

        claim_level = item.get("claim_level", CLAIM_DERIVED)
        if claim_level not in CLAIM_LEVEL_RULES:
            raise ConfigurationError(
                f"{where}: claim_level must be one of {sorted(CLAIM_LEVEL_RULES)}"
            )

        renderer = item.get("renderer")
        if generator == GENERATOR_DETERMINISTIC and not renderer:
            raise ConfigurationError(f"{where}: deterministic sections must name a renderer")
        if generator == GENERATOR_LLM and not item.get("instructions"):
            raise ConfigurationError(f"{where}: model-generated sections must carry instructions")

        sections.append(
            SectionSpec(
                id=str(section_id),
                heading=str(item.get("heading", section_id)),
                requires=_as_tuple(item.get("requires"), where=f"{where}.requires"),
                generator=generator,
                claim_level=claim_level,
                instructions=str(item.get("instructions", "")).strip(),
                tables=_as_tuple(item.get("tables"), where=f"{where}.tables"),
                renderer=renderer,
                max_words=item.get("max_words"),
                closing_note=str(item.get("closing_note", "")).strip(),
            )
        )

    return ReportSpec(
        report_type=str(raw["report_type"]),
        title=str(raw["title"]),
        product=str(raw["product"]),
        regulatory_basis=str(raw.get("regulatory_basis", "")),
        sections=tuple(sections),
        source_path=str(path),
        metadata={k: v for k, v in raw.items() if k not in
                  {"report_type", "title", "product", "regulatory_basis", "sections"}},
    )

def check_against_analyses(spec: ReportSpec, available: Iterable[str]) -> None:
    have = set(available)
    missing = sorted(set(spec.required_fact_ids) - have)
    if missing:
        raise EvidenceNotFoundError(
            f"{spec.report_type} requires facts that no analysis produces: "
            + ", ".join(missing)
        )

def configuration_facts(spec: ReportSpec) -> list[Fact]:
    method = f"supplied in report configuration {Path(spec.source_path).name}"
    facts = [
        Fact(id="product.name", label="Product", value=spec.product,
             grain=GRAIN_NONE, method=method),
        Fact(id="meta.report_title", label="Report title", value=spec.title,
             grain=GRAIN_NONE, method=method),
        Fact(id="meta.report_type", label="Report type", value=spec.report_type.upper(),
             grain=GRAIN_NONE, method=method),
    ]
    if spec.regulatory_basis:
        facts.append(
            Fact(id="meta.regulatory_basis", label="Regulatory basis",
                 value=spec.regulatory_basis, grain=GRAIN_NONE, method=method)
        )
    return facts
