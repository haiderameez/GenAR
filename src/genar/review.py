from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .errors import ReviewFileError
from .facts import FactStore
from .generate import SectionDraft
from .verify import VerificationResult

PENDING = "pending"
APPROVED = "approved"
FLAGGED = "flagged"
DECISIONS = (PENDING, APPROVED, FLAGGED)

GATE_ADVISORY = "advisory"
GATE_STRICT = "strict"

SAMPLE_CASE_IDS = 10

ANALYSIS_INSTRUCTIONS = (
    "Review each figure below against its 'method' line. Set 'decision' to "
    f"'{APPROVED}' or '{FLAGGED}' and add a 'note' if useful. A flagged figure is "
    "withheld from every section that declared it, and those sections will not be "
    "generated. Decisions persist across runs; if a figure's value changes, its "
    "decision resets to pending and the previous value is recorded."
)

SECTION_INSTRUCTIONS = (
    "Review each generated section against its verification record. Set 'decision' "
    f"to '{APPROVED}' or '{FLAGGED}'. In strict mode the report will not render "
    "until every section is approved; in advisory mode a flagged section is "
    "excluded and the report is marked as not reviewed."
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _summarise(value: Any) -> Any:
    from .facts import Distribution

    if isinstance(value, Distribution):
        return {"strata": [list(item) for item in value.items[:20]], "total": value.total}
    if isinstance(value, list):
        return f"{len(value)} records (not expanded)"
    if isinstance(value, dict):
        return value
    return value

@dataclass
class ReviewFile:
    path: Path
    stage: str
    instructions: str
    entries: dict[str, dict[str, Any]]
    meta: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "ReviewFile | None":
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReviewFileError(f"{path} is not valid JSON: {exc}") from exc
        return cls(
            path=path,
            stage=data.get("stage", ""),
            instructions=data.get("instructions", ""),
            entries=data.get("entries", {}),
            meta=data.get("meta", {}),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "stage": self.stage,
                    "instructions": self.instructions,
                    "meta": {**self.meta, "written_at": _now()},
                    "entries": self.entries,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )

    def decision(self, key: str) -> str:
        return self.entries.get(key, {}).get("decision", PENDING)

    def note(self, key: str) -> str:
        return self.entries.get(key, {}).get("note", "")

    def by_decision(self, decision: str) -> tuple[str, ...]:
        return tuple(key for key in self.entries if self.decision(key) == decision)

def _carry_forward(previous: ReviewFile | None, key: str, fingerprint: str) -> dict[str, Any]:
    if previous is None or key not in previous.entries:
        return {"decision": PENDING, "note": ""}
    entry = previous.entries[key]
    if entry.get("fingerprint") != fingerprint:
        return {
            "decision": PENDING,
            "note": entry.get("note", ""),
            "previous_decision": entry.get("decision", PENDING),
            "changed_since_review": True,
        }
    return {"decision": entry.get("decision", PENDING), "note": entry.get("note", "")}

def write_analysis_review(
    store: FactStore, path: Path, *, report_type: str, fact_ids: Iterable[str] | None = None
) -> ReviewFile:
    previous = ReviewFile.load(path)
    wanted = list(fact_ids) if fact_ids is not None else list(store.ids())

    entries: dict[str, dict[str, Any]] = {}
    for fact_id in sorted(set(wanted)):
        fact = store.get(fact_id)
        fingerprint = json.dumps(_summarise(fact.value), sort_keys=True, default=str)
        entries[fact_id] = {
            **_carry_forward(previous, fact_id, fingerprint),
            "label": fact.label,
            "value": _summarise(fact.value),
            "grain": fact.grain,
            "scope": fact.scope,
            "unit": fact.unit,
            "status": fact.status,
            "method": fact.method,
            "cases_behind": len(fact.case_ids),
            "sample_case_ids": list(fact.case_ids[:SAMPLE_CASE_IDS]),
            "fingerprint": fingerprint,
        }

    review = ReviewFile(
        path=path, stage="analysis", instructions=ANALYSIS_INSTRUCTIONS,
        entries=entries, meta={"report_type": report_type},
    )
    review.save()
    return review

def write_section_review(
    drafts: Sequence[SectionDraft],
    results: dict[str, VerificationResult],
    path: Path,
    *,
    report_type: str,
) -> ReviewFile:
    previous = ReviewFile.load(path)
    entries: dict[str, dict[str, Any]] = {}
    for draft in drafts:
        result = results.get(draft.section_id)
        entries[draft.section_id] = {
            **_carry_forward(previous, draft.section_id, draft.prompt_sha256),
            "heading": draft.heading,
            "model": draft.model,
            "text": draft.text,
            "fact_ids": list(draft.fact_ids),
            "verification": result.to_dict() if result else None,
            "fingerprint": draft.prompt_sha256,
        }

    review = ReviewFile(
        path=path, stage="sections", instructions=SECTION_INSTRUCTIONS,
        entries=entries, meta={"report_type": report_type},
    )
    review.save()
    return review

@dataclass(frozen=True)
class GateOutcome:
    blocked: tuple[str, ...]
    pending: tuple[str, ...]
    approved: tuple[str, ...]

    @property
    def clear(self) -> bool:
        return not self.blocked

def apply_gate(review: ReviewFile | None, keys: Iterable[str], mode: str) -> GateOutcome:
    keys = list(keys)
    if review is None:
        return GateOutcome(
            blocked=tuple(keys) if mode == GATE_STRICT else (), pending=tuple(keys), approved=()
        )

    flagged = [key for key in keys if review.decision(key) == FLAGGED]
    pending = [key for key in keys if review.decision(key) == PENDING]
    approved = [key for key in keys if review.decision(key) == APPROVED]
    blocked = flagged + (pending if mode == GATE_STRICT else [])
    return GateOutcome(tuple(blocked), tuple(pending), tuple(approved))

def approval_banner(outcome: GateOutcome, mode: str) -> str:
    if outcome.blocked:
        return (
            "**NOT APPROVED.** Items were flagged or left unreviewed at the human review "
            "gate and have been withheld from this document."
        )
    if outcome.pending:
        return (
            f"**REVIEW PENDING.** {len(outcome.pending)} item(s) have not been reviewed. "
            "This document was generated in advisory mode and has no reviewer approval."
        )
    return "**Reviewed.** All figures and sections in this document were approved at the human review gates."
