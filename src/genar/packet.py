from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import EvidenceNotFoundError
from .facts import Distribution, Fact, FactStore, format_count, format_percent
from .spec import ReportSpec, SectionSpec


MAX_DISTRIBUTION_ROWS = 12

SCOPE_WORDS = {
    "deduplicated": "latest version of each case only",
    "all_rows": "every row in the source file, superseded case versions included",
}


def unit_for(count: int, unit: str | None) -> str:
    if not unit or count != 1:
        return unit or ""
    return unit[:-1] if unit.endswith("s") else unit

def describe_value(fact: Fact, store: FactStore) -> list[str]:
    header = f"[{fact.id}] {fact.label}"

    if not fact.is_available:
        return [f"{header}: {fact.absence_phrase}", f"    reason: {fact.method}"]

    lines: list[str] = []
    value = fact.value

    if isinstance(value, Distribution):

        rows = value.items if value.ordered else value.items[:MAX_DISTRIBUTION_ROWS]
        lines.append(f"{header} (of {format_count(value.total)} {fact.unit}):")
        for label, count in rows:
            share = format_percent(count, value.total)
            lines.append(f"    {label}: {format_count(count)} {unit_for(count, fact.unit)} ({share})")
        if len(rows) < len(value.items):
            lines.append(
                "    (smaller entries are omitted here; the full breakdown is tabulated "
                "in the report and must not be summarised beyond the rows above)"
            )
    elif isinstance(value, bool):
        lines.append(f"{header}: {'yes' if value else 'no'}")
    elif isinstance(value, int):
        text = f"{header}: {format_count(value)} {unit_for(value, fact.unit)}".rstrip()
        if fact.denominator_id and fact.denominator_id in store:
            base = store.get(fact.denominator_id)
            if isinstance(base.value, int):
                text += f" ({format_percent(value, base.value)} of {format_count(base.value)})"
        lines.append(text)
    elif isinstance(value, (list, dict)):

        lines.append(f"{header}: rendered directly from data; not summarised here")
    else:
        lines.append(f"{header}: {value}")

    if fact.grain == "event":
        lines.append(f"    counted as: reaction events, {SCOPE_WORDS.get(fact.scope, fact.scope)}")
    elif fact.grain == "case":
        lines.append("    counted as: cases")
    lines.append(f"    basis: {fact.method}")
    return lines

@dataclass(frozen=True)
class Packet:

    section_id: str
    heading: str
    report_title: str
    product: str
    claim_level: str
    claim_rule: str
    instructions: str
    facts: tuple[Fact, ...]
    max_words: int | None = None

    @property
    def absent_facts(self) -> tuple[Fact, ...]:
        return tuple(fact for fact in self.facts if not fact.is_available)

    def render(self, store: FactStore) -> str:
        blocks: list[str] = [
            f"Report: {self.report_title} for {self.product}",
            f"Section to write: {self.heading}",
            "",
            "APPROVED FIGURES",
            "These are the only figures you may state. Each is written here in the "
            "form you must use.",
            "",
        ]
        for fact in self.facts:
            blocks.extend(describe_value(fact, store))
            blocks.append("")

        absent = self.absent_facts
        if absent:
            blocks.append(
                "The items marked as not provided or not available above must be "
                "stated as unavailable, using the reason given. None of them is zero."
            )
            blocks.append("")

        blocks.append("WHAT THIS SECTION MUST DO")
        blocks.append(self.instructions)
        blocks.append("")
        blocks.append(f"CLAIM LEVEL: {self.claim_level}")
        blocks.append(self.claim_rule)
        if self.max_words:
            blocks.append("")
            blocks.append(f"Length: at most {self.max_words} words.")
        return "\n".join(blocks).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "heading": self.heading,
            "claim_level": self.claim_level,
            "fact_ids": [fact.id for fact in self.facts],
            "max_words": self.max_words,
        }

def assemble(spec: ReportSpec, section: SectionSpec, store: FactStore) -> Packet:
    facts = store.require(section.requires)

    for fact in facts:
        if fact.denominator_id and fact.denominator_id not in store:
            raise EvidenceNotFoundError(
                f"{fact.id} names denominator {fact.denominator_id}, which is not available"
            )

    return Packet(
        section_id=section.id,
        heading=section.heading,
        report_title=spec.title,
        product=spec.product,
        claim_level=section.claim_level,
        claim_rule=section.claim_rule,
        instructions=section.instructions,
        facts=tuple(facts),
        max_words=section.max_words,
    )
