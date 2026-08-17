from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .errors import RendererNotFoundError
from .facts import Distribution, Fact, FactStore, format_count, format_percent
from .spec import ReportSpec, SectionSpec
from .verify import VerificationResult


CASE_INDEX_INLINE_ROWS = 25


@dataclass
class RenderContext:
    spec: ReportSpec
    output_dir: Path
    generated_at: str = ""
    manifest: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)

def distribution_table(fact: Fact) -> str:
    dist: Distribution = fact.value
    unit = fact.unit or "cases"
    rows = [
        (label, format_count(count), format_percent(count, dist.total))
        for label, count in dist.items
    ]
    table = markdown_table([fact.label, unit.capitalize(), f"Share of {format_count(dist.total)}"], rows)
    return f"{table}\n\n*Basis: {fact.method}.*"

def render_tables(section: SectionSpec, store: FactStore) -> str:
    blocks: list[str] = []
    for fact_id in section.tables:
        fact = store.get(fact_id)
        if not fact.is_available:
            blocks.append(f"**{fact.label}:** {fact.absence_phrase} ({fact.method}).")
        elif isinstance(fact.value, Distribution):
            blocks.append(distribution_table(fact))
    return "\n\n".join(blocks)

Renderer = Callable[[SectionSpec, FactStore, RenderContext], str]
_RENDERERS: dict[str, Renderer] = {}


def renderer(name: str) -> Callable[[Renderer], Renderer]:
    def decorate(fn: Renderer) -> Renderer:
        _RENDERERS[name] = fn
        return fn

    return decorate

def get_renderer(name: str) -> Renderer:
    try:
        return _RENDERERS[name]
    except KeyError:
        raise RendererNotFoundError(
            f"unknown renderer {name!r}; available: {', '.join(sorted(_RENDERERS))}"
        ) from None

def renderer_names() -> tuple[str, ...]:
    return tuple(sorted(_RENDERERS))

@renderer("header_block")
def _header_block(section: SectionSpec, store: FactStore, ctx: RenderContext) -> str:
    def value(fact_id: str) -> str:
        if fact_id not in store:
            return "not specified"
        fact = store.get(fact_id)
        if not fact.is_available:
            return fact.absence_phrase
        return format_count(fact.value) if isinstance(fact.value, int) else str(fact.value)

    rows = [
        ("Product", value("product.name")),
        ("Report type", value("meta.report_type")),
        ("Regulatory basis", value("meta.regulatory_basis")),
        ("Reporting period", value("period.label")),
        ("Period length", f"{value('period.days')} days"),
        ("Data cut-off", value("period.end")),
        ("Cases in this report", f"{value('cases.total')} cases"),
        ("Application / NDA number", "not provided in the supplied dataset"),
    ]
    return markdown_table(["Field", "Value"], rows)

@renderer("absence_statement")
def _absence_statement(section: SectionSpec, store: FactStore, ctx: RenderContext) -> str:
    parts: list[str] = []
    for fact_id in section.requires:
        fact = store.get(fact_id)
        if fact.is_available:
            continue
        parts.append(
            f"**{fact.label}**: {fact.absence_phrase}.\n\n"
            f"No such information accompanied the dataset supplied for this reporting "
            f"period, and none has been inferred. This is a statement about the "
            f"information available to this report, not a statement that no actions "
            f"occurred.\n\n*Basis: {fact.method}.*"
        )
    return "\n\n".join(parts)

@renderer("data_quality")
def _data_quality(section: SectionSpec, store: FactStore, ctx: RenderContext) -> str:
    blocks: list[str] = []

    if "meta.dataset_provenance" in store:
        provenance = store.get("meta.dataset_provenance").value
        blocks.append(
            markdown_table(
                ["Field", "Value"],
                [
                    ("Source rows read", format_count(provenance["rows"])),
                    ("Cases after version resolution", format_count(provenance["cases"])),
                    ("Source SHA-256", f"`{provenance['sha256'][:16]}...`"),
                    ("Report generated", ctx.generated_at),
                ],
            )
        )

    if "meta.data_quality" in store:
        findings = store.get("meta.data_quality").value
        blocks.append(
            markdown_table(
                ["Check", "Severity", "Cases", "Note"],
                [
                    (f["summary"], f["severity"], format_count(f["count"]), f["detail"] or "not applicable")
                    for f in findings
                ],
            )
        )

    blocks.append(
        "*Counts described as 'cases' are counts of distinct case identifiers at their "
        "latest supplied version. Counts described as 'reaction events' are counts of "
        "individual reaction records. The two are reported separately throughout and are "
        "not interchangeable.*"
    )
    return "\n\n".join(blocks)

@renderer("case_listing")
def _case_listing(section: SectionSpec, store: FactStore, ctx: RenderContext) -> str:
    fact = store.get("cases.listing")
    rows: list[dict[str, Any]] = fact.value
    total = len(rows)

    csv_path = ctx.output_dir / "case_index.csv"
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)

    headers = ["Case ID", "Received", "Country", "Sex", "Age group", "Seriousness", "Expedited", "Reactions", "Outcomes"]
    shown = [
        (
            row["case_id"], row["received"], row["country"], row["sex"], row["age_group"],
            row["serious"], row["expedited"], row["reactions"], row["outcomes"],
        )
        for row in rows[:CASE_INDEX_INLINE_ROWS]
    ]
    note = (
        f"The first {format_count(len(shown))} of {format_count(total)} cases are shown. "
        f"The complete index is written to `{csv_path.name}` alongside this report each "
        "time it is generated. It is not included in the submission package: it is a "
        "restatement of the supplied dataset, which the data usage notice asks not to be "
        "redistributed."
    )
    return f"{note}\n\n{markdown_table(headers, shown)}\n\n*Basis: {fact.method}.*"

def render_section_body(
    section: SectionSpec, store: FactStore, ctx: RenderContext, text: str | None
) -> str:
    if section.uses_llm:
        body = (text or "").strip()
    else:
        body = get_renderer(section.renderer or "")(section, store, ctx).strip()

    tables = render_tables(section, store)
    parts = [part for part in (body, tables, section.closing_note.strip()) if part]
    return "\n\n".join(parts)

def evidence_index(store: FactStore, used_ids: Iterable[str]) -> str:
    rows = []
    for fact_id in sorted(set(used_ids)):
        fact = store.get(fact_id)
        if fact.is_available:
            if isinstance(fact.value, Distribution):
                summary = f"{format_count(len(fact.value.items))} strata of {format_count(fact.value.total)}"
            elif isinstance(fact.value, int):
                summary = format_count(fact.value)
            elif isinstance(fact.value, (list, dict)):
                summary = f"{format_count(len(fact.value))} records"
            else:
                summary = str(fact.value)
        else:
            summary = fact.absence_phrase
        cases = format_count(len(fact.case_ids)) if fact.case_ids else "not applicable"
        rows.append((f"`{fact.id}`", fact.label, summary, cases, fact.method))
    return markdown_table(["Fact", "Figure", "Value", "Cases behind it", "How it was computed"], rows)

def verification_table(results: Sequence[VerificationResult]) -> str:
    rows = [
        (
            result.section_id,
            format_count(result.claims_checked),
            f"{result.grounding_score * 100:.1f}%",
            "pass" if result.passed else f"{len(result.violations)} violation(s)",
        )
        for result in results
    ]
    return markdown_table(["Section", "Claims checked", "Grounded", "Result"], rows)

def render_report(
    spec: ReportSpec,
    store: FactStore,
    ctx: RenderContext,
    section_text: dict[str, str],
    results: Sequence[VerificationResult] = (),
) -> str:
    lines: list[str] = [
        f"# {spec.title}",
        "",
        f"**{spec.product}**, reporting period "
        f"{store.get('period.label').value if 'period.label' in store else 'not determined'}",
        "",
        "> Generated by the GenAR reporting pipeline. Every figure in this document was "
        "computed deterministically from the source dataset and verified against the "
        "evidence supplied to the section that states it. This report requires review and "
        "approval by a qualified pharmacovigilance reviewer before any regulatory use.",
        "",
    ]

    for index, section in enumerate(spec.sections, start=1):
        lines.append(f"## {index}. {section.heading}")
        lines.append("")
        lines.append(render_section_body(section, store, ctx, section_text.get(section.id)))
        lines.append("")

    used = [fact_id for section in spec.sections for fact_id in (*section.requires, *section.tables)]
    lines.extend(["---", "", "## Appendix A. Evidence Index", "",
                  "Every figure above traces to one of these. "
                  "\"Cases behind it\" is the number of case records the figure was computed from.",
                  "", evidence_index(store, used), ""])

    if results:
        lines.extend(["## Appendix B. Grounding Verification", "",
                      "Each model-written section was checked automatically: every number and date "
                      "in its text was matched against the evidence that section was given, and the "
                      "noun attached to each figure was checked against the population it counts.",
                      "", verification_table(results), ""])

    if ctx.manifest:
        lines.extend(["## Appendix C. Run Manifest", "",
                      "What produced this document.", "",
                      markdown_table(["Field", "Value"],
                                     [(k, f"`{v}`") for k, v in ctx.manifest.items()]), ""])

    return "\n".join(lines).rstrip() + "\n"
