from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from genar.analyses import compute
from genar.config import REPORTS_DIR, resolve_dataset
from genar.facts import FactStore
from genar.loader import load_dataset
from genar.packet import assemble
from genar.spec import configuration_facts, load_spec
from genar.validate import provenance_fact, quality_fact, validate


@pytest.fixture(scope="session")
def dataset():
    try:
        path = resolve_dataset()
    except FileNotFoundError as exc:
        pytest.skip(str(exc))
    return load_dataset(path)

@pytest.fixture(scope="session")
def quality(dataset):
    return validate(dataset)

@pytest.fixture(scope="session")
def facts(dataset):
    return compute(dataset)

@pytest.fixture(scope="session")
def spec():
    return load_spec(REPORTS_DIR / "pader.yaml")

@pytest.fixture(scope="session")
def store(dataset, facts, quality, spec):
    return FactStore(
        [*facts, *configuration_facts(spec), quality_fact(quality), provenance_fact(dataset)]
    )

@pytest.fixture(scope="session")
def packets(spec, store):
    return {
        section.id: assemble(spec, section, store)
        for section in spec.sections
        if section.uses_llm
    }
