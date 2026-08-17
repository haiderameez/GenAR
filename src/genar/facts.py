from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .errors import EvidenceNotFoundError, FactError


GRAIN_CASE = "case"
GRAIN_EVENT = "event"
GRAIN_NONE = "none"
GRAINS = frozenset({GRAIN_CASE, GRAIN_EVENT, GRAIN_NONE})


SCOPE_DEDUPLICATED = "deduplicated"
SCOPE_ALL_ROWS = "all_rows"
SCOPES = frozenset({SCOPE_DEDUPLICATED, SCOPE_ALL_ROWS})


STATUS_COMPUTED = "computed"
STATUS_NOT_PROVIDED = "not_provided"
STATUS_NOT_AVAILABLE = "not_available"
STATUSES = frozenset({STATUS_COMPUTED, STATUS_NOT_PROVIDED, STATUS_NOT_AVAILABLE})


NAMESPACE_GRAIN: Mapping[str, str] = {
    "cases": GRAIN_CASE,
    "events": GRAIN_EVENT,
    "period": GRAIN_NONE,
    "product": GRAIN_NONE,
    "meta": GRAIN_NONE,
    "absent": GRAIN_NONE,
}


PERCENT_DP = 1


def format_percent(numerator: float, denominator: float) -> str:
    if not denominator:
        return "not calculable"
    return f"{numerator / denominator * 100:.{PERCENT_DP}f}%"

def format_count(value: int) -> str:
    return f"{value:,}"

@dataclass(frozen=True)
class Distribution:

    items: tuple[tuple[str, int], ...]
    total: int

    ordered: bool = False
    support: Mapping[str, tuple[str, ...]] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_counts(
        cls,
        counts: Mapping[str, int],
        total: int,
        support: Mapping[str, Iterable[str]] | None = None,
        *,
        order: tuple[str, ...] | None = None,
    ) -> "Distribution":
        if order is not None:
            items = tuple((label, counts.get(label, 0)) for label in order if label in counts)
        else:
            items = tuple(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        return cls(
            items=items,
            total=total,
            ordered=order is not None,
            support={k: tuple(v) for k, v in (support or {}).items()},
        )

    def top(self, n: int) -> "Distribution":
        return Distribution(
            items=self.items[:n], total=self.total, ordered=self.ordered, support=self.support
        )

    def count_for(self, label: str) -> int:
        for name, count in self.items:
            if name == label:
                return count
        return 0

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(label for label, _ in self.items)

    @property
    def counts(self) -> tuple[int, ...]:
        return tuple(count for _, count in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {"items": [list(item) for item in self.items], "total": self.total}

@dataclass(frozen=True)
class Fact:

    id: str
    label: str
    value: Any
    grain: str
    method: str
    unit: str | None = None
    scope: str | None = None
    case_ids: tuple[str, ...] = field(default=(), repr=False)
    status: str = STATUS_COMPUTED

    denominator_id: str | None = None

    def __post_init__(self) -> None:
        if self.grain not in GRAINS:
            raise FactError(f"{self.id}: grain must be one of {sorted(GRAINS)}, got {self.grain!r}")
        if self.status not in STATUSES:
            raise FactError(f"{self.id}: unknown status {self.status!r}")

        namespace = self.id.split(".")[0]
        required = NAMESPACE_GRAIN.get(namespace)
        if required is not None and self.grain != required:
            raise FactError(
                f"{self.id}: ids under '{namespace}.' are {required}-grain, "
                f"but this fact declares grain={self.grain!r}"
            )

        if self.grain == GRAIN_EVENT and self.scope not in SCOPES:
            raise FactError(
                f"{self.id}: event-grain facts must declare scope "
                f"({sorted(SCOPES)}); got {self.scope!r}"
            )
        if self.grain != GRAIN_EVENT and self.scope is not None:
            raise FactError(f"{self.id}: scope only applies to event-grain facts")

        if self.status is STATUS_COMPUTED and self.grain != GRAIN_NONE and not self.unit:
            raise FactError(f"{self.id}: counting facts must declare a unit")

        if self.denominator_id == self.id:
            raise FactError(f"{self.id}: a fact cannot be its own denominator")

        if self.status != STATUS_COMPUTED:

            if self.value is not None:
                raise FactError(f"{self.id}: status {self.status} requires value=None")
            if self.case_ids:
                raise FactError(f"{self.id}: status {self.status} cannot cite cases")

    @property
    def is_available(self) -> bool:
        return self.status == STATUS_COMPUTED

    @property
    def absence_phrase(self) -> str:
        if self.status == STATUS_NOT_PROVIDED:
            return "not provided in the supplied dataset"
        if self.status == STATUS_NOT_AVAILABLE:
            return "not available from the supplied dataset"
        raise FactError(f"{self.id} is available; it has no absence phrase")

    def to_dict(self) -> dict[str, Any]:
        value: Any = self.value
        if isinstance(value, Distribution):
            value = value.to_dict()
        return {
            "id": self.id,
            "label": self.label,
            "value": value,
            "grain": self.grain,
            "scope": self.scope,
            "unit": self.unit,
            "method": self.method,
            "status": self.status,
            "denominator_id": self.denominator_id,
            "case_count": len(self.case_ids),
        }

class FactStore:

    def __init__(self, facts: Iterable[Fact] = ()) -> None:
        self._facts: dict[str, Fact] = {}
        for fact in facts:
            self.add(fact)

    def add(self, fact: Fact) -> None:
        if fact.id in self._facts:
            raise FactError(f"duplicate fact id: {fact.id}")
        self._facts[fact.id] = fact

    def __contains__(self, fact_id: object) -> bool:
        return fact_id in self._facts

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self):
        return iter(self._facts.values())

    def get(self, fact_id: str) -> Fact:
        try:
            return self._facts[fact_id]
        except KeyError:
            raise EvidenceNotFoundError(f"no such fact: {fact_id}") from None

    def require(self, fact_ids: Iterable[str]) -> list[Fact]:
        wanted = list(fact_ids)
        missing = [fact_id for fact_id in wanted if fact_id not in self._facts]
        if missing:
            raise EvidenceNotFoundError(
                "report configuration requires facts that no analysis produced: "
                + ", ".join(sorted(missing))
            )
        return [self._facts[fact_id] for fact_id in wanted]

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._facts))

    def to_dict(self) -> dict[str, Any]:
        return {fact_id: fact.to_dict() for fact_id, fact in sorted(self._facts.items())}
