from __future__ import annotations

from genar.analyses import compute, registered_fact_ids
from genar.config import REPORTS_DIR
from genar.facts import FactStore
from genar.packet import assemble
from genar.render import RenderContext, render_report
from genar.spec import check_against_analyses, configuration_facts, load_spec
from genar.validate import provenance_fact, quality_fact
from genar.verify import verify_section

from .fakes import TemplateClient

SECOND_REPORT = REPORTS_DIR / "psur_mini.yaml"


def test_the_second_report_type_needs_no_new_analysis():
    spec = load_spec(SECOND_REPORT)
    available = {
        *registered_fact_ids(),
        *(fact.id for fact in configuration_facts(spec)),
        "meta.data_quality",
        "meta.dataset_provenance",
    }
    check_against_analyses(spec, available)

def test_the_two_report_types_share_no_sections(spec):
    other = load_spec(SECOND_REPORT)
    assert {s.id for s in spec.sections}.isdisjoint({s.id for s in other.sections})
    assert other.report_type != spec.report_type
    assert other.regulatory_basis != spec.regulatory_basis

def test_the_two_report_types_share_most_of_their_evidence(spec):
    other = load_spec(SECOND_REPORT)
    shared = set(spec.required_fact_ids) & set(other.required_fact_ids)
    assert len(shared) >= 20
    assert "cases.total" in shared and "events.by_outcome" in shared

def test_the_second_report_type_generates_and_verifies(dataset, quality, tmp_path):
    spec = load_spec(SECOND_REPORT)
    store = FactStore(
        [
            *compute(dataset, [f for f in spec.required_fact_ids
                               if not f.startswith(("meta.", "product."))]),
            *configuration_facts(spec),
            quality_fact(quality),
            provenance_fact(dataset),
        ]
    )

    client = TemplateClient()
    text, results = {}, []
    for section in spec.sections:
        if not section.uses_llm:
            continue
        packet = assemble(spec, section, store)
        generated = client.complete("", packet.render(store)).text
        result = verify_section(generated, packet, store)
        assert result.passed, f"{section.id}: {[v.detail for v in result.violations]}"
        text[section.id] = generated
        results.append(result)

    ctx = RenderContext(spec=spec, output_dir=tmp_path)
    document = render_report(spec, store, ctx, text, results)

    assert "Periodic Safety Update Report" in document
    assert "EMA GVP Module VII" in document
    assert "Line Listing of Cases" in document
    assert "Periodic Adverse Drug Experience" not in document

def test_a_report_only_pays_for_the_analyses_it_declares(dataset):
    everything = compute(dataset)
    subset = compute(dataset, ["cases.total", "cases.serious", "cases.non_serious"])
    assert len(subset) == 3
    assert len(everything) > len(subset)
