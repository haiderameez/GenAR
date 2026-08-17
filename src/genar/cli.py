from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__
from .analyses import compute, registered_fact_ids
from .config import (
    LLMSettings,
    OUTPUT_DIR,
    REPORTS_DIR,
    REVIEW_DIR,
    load_dotenv,
    resolve_dataset,
)
from .facts import FactStore
from .generate import generate_section, load_system_prompt
from .llm import GeminiClient
from .loader import load_dataset
from .packet import assemble
from .render import RenderContext, render_report
from .review import (
    GATE_ADVISORY,
    GATE_STRICT,
    ReviewFile,
    apply_gate,
    approval_banner,
    write_analysis_review,
    write_section_review,
)
from .errors import GenarError
from .spec import check_against_analyses, configuration_facts, load_spec
from .validate import provenance_fact, quality_fact, validate
from .verify import summarise, verify_section

LOGGER_NAME = "genar"
logger = logging.getLogger(LOGGER_NAME)

def configure_logging(level: int = logging.INFO, stream=None) -> None:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    target = logging.getLogger(LOGGER_NAME)
    for existing in list(target.handlers):
        target.removeHandler(existing)
    target.addHandler(handler)
    target.setLevel(level)
    target.propagate = False

def _spec_path(report: str) -> Path:
    candidate = Path(report)
    return candidate if candidate.suffix in {".yaml", ".yml"} else REPORTS_DIR / f"{report}.yaml"

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="genar", description=__doc__)
    parser.add_argument("command", choices=["run", "analyze", "list-facts"],
                        help="run: full pipeline. analyze: stop after the analysis review gate.")
    parser.add_argument("--report", default="pader", help="report type name or path to a YAML spec")
    parser.add_argument("--data", default=None, help="path to the ICSR line listing (.xlsx or .csv)")
    parser.add_argument("--model", default=None, help="override the model id")
    parser.add_argument("--sections", default=None,
                        help="comma-separated section ids to regenerate; others are reused from "
                             "the previous run, so fixing one section costs one call")
    parser.add_argument("--gate", default=GATE_ADVISORY, choices=[GATE_ADVISORY, GATE_STRICT],
                        help="advisory: flagged items block. strict: anything unapproved blocks.")
    parser.add_argument("--out", default=None, help="path for the rendered report")
    parser.add_argument("--verbose", action="store_true", help="emit debug-level logging")
    parser.add_argument("--quiet", action="store_true", help="suppress progress logging")
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.quiet:
        level = logging.ERROR
    elif args.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO
    configure_logging(level)

    from_file = load_dotenv()
    if from_file:
        logger.info(f"env           : loaded {', '.join(sorted(from_file))} from .env")

    if args.command == "list-facts":
        for fact_id in registered_fact_ids():
            print(fact_id)
        return 0

    spec = load_spec(_spec_path(args.report))

    available = {
        *registered_fact_ids(),
        *(fact.id for fact in configuration_facts(spec)),
        "meta.data_quality",
        "meta.dataset_provenance",
    }
    check_against_analyses(spec, available)
    logger.info(f"report type   : {spec.report_type} ({len(spec.sections)} sections, "
         f"{spec.llm_section_count} model-written)")

    data_path = resolve_dataset(args.data)
    dataset = load_dataset(data_path)
    logger.info(f"dataset       : {data_path.name} -> {len(dataset.all_rows)} rows, "
         f"{len(dataset.cases)} cases")

    quality = validate(dataset)
    quality.raise_if_invalid()
    warnings = [f for f in quality.findings if f.severity == "warning"]
    logger.info(f"validation    : {len(quality.findings)} checks, {len(warnings)} warnings")

    data_fact_ids = [
        fact_id for fact_id in spec.required_fact_ids
        if not fact_id.startswith(("meta.", "product."))
    ]
    store = FactStore(
        [
            *compute(dataset, data_fact_ids),
            *configuration_facts(spec),
            quality_fact(quality),
            provenance_fact(dataset),
        ]
    )
    logger.info(f"analysis      : {len(store)} facts computed for the declared sections")

    analysis_review_path = REVIEW_DIR / f"{spec.report_type}_analysis_review.json"
    write_analysis_review(store, analysis_review_path,
                          report_type=spec.report_type, fact_ids=store.ids())
    analysis_review = ReviewFile.load(analysis_review_path)
    fact_gate = apply_gate(analysis_review, store.ids(), args.gate)
    logger.info(f"review gate 1 : {len(fact_gate.approved)} approved, {len(fact_gate.pending)} pending, "
         f"{len(fact_gate.blocked)} blocked -> {analysis_review_path.name}")

    if args.command == "analyze":
        logger.info("stopping after the analysis gate; review the file above, then re-run with 'run'.")
        return 0

    settings = LLMSettings()
    if args.model:
        settings = replace(settings, model=args.model)
    client = GeminiClient(settings)
    system_prompt = load_system_prompt()

    section_review_path = REVIEW_DIR / f"{spec.report_type}_section_review.json"
    previous_sections = ReviewFile.load(section_review_path)
    only = {s.strip() for s in args.sections.split(",")} if args.sections else None

    drafts, results, section_text = [], {}, {}
    blocked_facts = set(fact_gate.blocked)

    for section in spec.sections:
        if not section.uses_llm:
            continue

        starved = sorted(set(section.requires) & blocked_facts)
        if starved:
            section_text[section.id] = (
                "*This section was not generated: the following evidence was flagged or "
                "not approved at the analysis review gate: " + ", ".join(f"`{f}`" for f in starved) + ".*"
            )
            logger.info(f"  {section.id:24s} withheld ({len(starved)} blocked fact(s))")
            continue

        reuse = (
            previous_sections
            and only is not None
            and section.id not in only
            and section.id in previous_sections.entries
        )
        if reuse:
            section_text[section.id] = previous_sections.entries[section.id]["text"]
            logger.info(f"  {section.id:24s} reused from previous run")
            continue

        draft, packet = generate_section(spec, section, store, client, system_prompt=system_prompt)
        result = verify_section(draft.text, packet, store)
        drafts.append(draft)
        results[section.id] = result
        section_text[section.id] = draft.text
        status = "ok" if result.passed else f"{len(result.violations)} violation(s)"
        logger.info(f"  {section.id:24s} {len(draft.text.split()):4d} words, "
             f"{result.claims_checked:3d} claims, {status}")

    calls = getattr(client, "calls_made", 0)
    logger.info(f"generation    : {len(drafts)} section(s), {calls} model call(s) via {client.name}")

    overall = summarise(results.values())
    logger.info(f"verification  : {overall['claims_grounded']}/{overall['claims_checked']} claims grounded, "
         f"{overall['violations']} violation(s)")
    for section_id, result in results.items():
        for violation in result.violations:
            logger.warning(f"  ! {section_id}: [{violation.kind}] {violation.detail}")

    write_section_review(drafts, results, section_review_path, report_type=spec.report_type)
    section_review = ReviewFile.load(section_review_path)
    section_gate = apply_gate(section_review, [d.section_id for d in drafts], args.gate)
    logger.info(f"review gate 2 : {len(section_gate.approved)} approved, {len(section_gate.pending)} pending, "
         f"{len(section_gate.blocked)} blocked -> {section_review_path.name}")

    for section_id in section_gate.blocked:
        section_text[section_id] = (
            "*This section was flagged at the section review gate and is withheld from this document.*"
        )

    if args.gate == GATE_STRICT and (fact_gate.blocked or section_gate.blocked):
        logger.warning("strict mode: unreviewed or flagged items remain; the report was not rendered.")
        return 1

    out_path = Path(args.out) if args.out else OUTPUT_DIR / "report_output.md"
    manifest = {
        "genar_version": __version__,
        "report_config": Path(spec.source_path).name,
        "dataset_sha256": dataset.source_sha256[:16],
        "system_prompt_sha256": _sha(system_prompt),
        "model": client.model,
        "model_calls_this_run": calls,
        "review_mode": args.gate,
    }
    ctx = RenderContext(spec=spec, output_dir=out_path.parent, manifest=manifest)
    document = render_report(spec, store, ctx, section_text, list(results.values()))

    banner = approval_banner(
        apply_gate(analysis_review, store.ids(), args.gate), args.gate
    )
    document = document.replace("> Generated by", f"> {banner}\n>\n> Generated by", 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")
    logger.info(f"rendered      : {out_path} ({len(document.splitlines())} lines)")
    return 0

def run() -> int:
    try:
        return main()
    except GenarError as exc:
        configure_logging()
        logger.error(f"{type(exc).__name__}: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    raise SystemExit(run())
