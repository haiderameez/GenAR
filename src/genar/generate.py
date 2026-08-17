from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import PROMPTS_DIR
from .errors import ConfigurationError
from .llm import LLMClient
from .packet import Packet, assemble
from .facts import FactStore
from .spec import ReportSpec, SectionSpec


def load_system_prompt(path: str | None = None) -> str:
    prompt_path = Path(path) if path else PROMPTS_DIR / "system.md"
    if not prompt_path.exists():
        raise ConfigurationError(f"system prompt not found: {prompt_path}")
    return prompt_path.read_text(encoding="utf-8").strip()

@dataclass(frozen=True)
class SectionDraft:

    section_id: str
    heading: str
    text: str
    model: str
    prompt_sha256: str
    fact_ids: tuple[str, ...]
    prompt: str = field(repr=False, default="")

    def to_dict(self, *, include_prompt: bool = False) -> dict[str, Any]:
        data = {
            "section_id": self.section_id,
            "heading": self.heading,
            "text": self.text,
            "model": self.model,
            "prompt_sha256": self.prompt_sha256,
            "fact_ids": list(self.fact_ids),
        }
        if include_prompt:
            data["prompt"] = self.prompt
        return data

def generate_section(
    spec: ReportSpec,
    section: SectionSpec,
    store: FactStore,
    client: LLMClient,
    *,
    system_prompt: str | None = None,
) -> tuple[SectionDraft, Packet]:
    if not section.uses_llm:
        raise ConfigurationError(f"{section.id} is a deterministic section; render it instead")

    packet = assemble(spec, section, store)
    system = system_prompt if system_prompt is not None else load_system_prompt()
    rendered = packet.render(store)

    completion = client.complete(system, rendered)
    return (
        SectionDraft(
            section_id=section.id,
            heading=section.heading,
            text=completion.text.strip(),
            model=completion.model,
            prompt_sha256=completion.prompt_sha256,
            fact_ids=tuple(fact.id for fact in packet.facts),
            prompt=rendered,
        ),
        packet,
    )
