from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .facts import (
    GRAIN_CASE,
    GRAIN_EVENT,
    Distribution,
    Fact,
    FactStore,
    format_count,
    format_percent,
)
from .packet import Packet


BANNED_PHRASES: tuple[str, ...] = (
    "safety signal",
    "emerging signal",
    "signal was identified",
    "no safety concern",
    "no new safety concern",
    "no safety issues",
    "causally related",
    "caused by the product",
    "attributable to",
    "is attributable",
    "confirms",
    "confirmed that",
    "proves",
    "demonstrates that",
    "consistent with the known safety profile",
    "known safety profile",
    "favourable safety profile",
    "favorable safety profile",
    "benefit-risk remains",
    "benefit-risk balance",
    "reassuring",
    "no action is required",
    "no further action",
    "over-represented",
    "overrepresented",
    "disproportionately affected",
    "warrants further",
)


CASE_NOUNS = frozenset(
    {"case", "cases", "report", "reports", "icsr", "icsrs", "patient", "patients"}
)
EVENT_NOUNS = frozenset(
    {"reaction", "reactions", "event", "events", "term", "terms", "occurrence", "occurrences"}
)

_SMALL_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
}

_CITATION = re.compile(r"\b\d+\s*CFR\s*[\d.]+(?:\([a-z0-9]+\))*", re.IGNORECASE)
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(%|per\s?cent(?:age)?)?", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")


@dataclass(frozen=True)
class Violation:
    kind: str
    detail: str
    excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail, "excerpt": self.excerpt}

@dataclass(frozen=True)
class MatchedClaim:

    text: str
    fact_ids: tuple[str, ...]
    case_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "fact_ids": list(self.fact_ids), "cases_behind": self.case_count}

@dataclass(frozen=True)
class VerificationResult:
    section_id: str
    claims_checked: int
    matched: tuple[MatchedClaim, ...]
    violations: tuple[Violation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def grounding_score(self) -> float:
        if not self.claims_checked:
            return 1.0
        ungrounded = sum(1 for v in self.violations if v.kind in _UNGROUNDED_KINDS)
        return max(0.0, (self.claims_checked - ungrounded) / self.claims_checked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "passed": self.passed,
            "claims_checked": self.claims_checked,
            "grounding_score": round(self.grounding_score, 4),
            "matched_claims": [claim.to_dict() for claim in self.matched],
            "violations": [violation.to_dict() for violation in self.violations],
        }

_UNGROUNDED_KINDS = {"ungrounded_number", "ungrounded_date"}


@dataclass
class AllowedRenderings:

    numbers: dict[float, set[str]] = field(default_factory=dict)
    percents: dict[str, set[str]] = field(default_factory=dict)
    words: dict[str, set[str]] = field(default_factory=dict)
    dates: dict[str, set[str]] = field(default_factory=dict)
    grain_by_number: dict[float, set[str]] = field(default_factory=dict)

    def allow_number(self, value: float, fact_id: str, grain: str) -> None:
        self.numbers.setdefault(float(value), set()).add(fact_id)
        self.grain_by_number.setdefault(float(value), set()).add(grain)
        if float(value).is_integer() and int(value) in _SMALL_WORDS:
            self.words.setdefault(_SMALL_WORDS[int(value)], set()).add(fact_id)

    def allow_percent(self, text: str, fact_id: str) -> None:
        self.percents.setdefault(text.lower(), set()).add(fact_id)

    def allow_date(self, text: str, fact_id: str) -> None:
        self.dates.setdefault(text, set()).add(fact_id)

def build_allowed(packet: Packet, store: FactStore) -> AllowedRenderings:
    allowed = AllowedRenderings()
    for fact in packet.facts:
        if not fact.is_available:
            continue
        _allow_fact(fact, store, allowed)
    return allowed

def _allow_text(text: str, fact_id: str, allowed: AllowedRenderings) -> None:
    for match in _ISO_DATE.finditer(text):
        allowed.allow_date(match.group(0), fact_id)
    masked = _ISO_DATE.sub(" ", text)
    for match in _NUMBER.finditer(masked):
        try:
            allowed.allow_number(float(match.group(1).replace(",", "")), fact_id, "none")
        except ValueError:
            continue

def _allow_fact(fact: Fact, store: FactStore, allowed: AllowedRenderings) -> None:
    _allow_text(fact.label, fact.id, allowed)
    value = fact.value

    if isinstance(value, Distribution):
        allowed.allow_number(value.total, fact.id, fact.grain)
        for label, count in value.items:
            allowed.allow_number(count, fact.id, fact.grain)
            allowed.allow_percent(format_percent(count, value.total), fact.id)
            _allow_text(label, fact.id, allowed)
        return

    if isinstance(value, bool):
        return

    if isinstance(value, (int, float)):
        allowed.allow_number(value, fact.id, fact.grain)
        if fact.denominator_id and fact.denominator_id in store:
            base = store.get(fact.denominator_id)
            if isinstance(base.value, (int, float)) and base.value:
                allowed.allow_percent(format_percent(value, base.value), fact.id)

                allowed.allow_number(base.value, base.id, base.grain)
        return

    if isinstance(value, str):
        _allow_text(value, fact.id, allowed)

def _excerpt(text: str, start: int, end: int, width: int = 70) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return ("..." if left else "") + text[left:right].replace("\n", " ") + ("..." if right < len(text) else "")

def _following_noun(text: str, position: int, lookahead: int = 60) -> str | None:
    for match in _WORD.finditer(text[position : position + lookahead]):
        word = match.group(0).lower()
        if word in CASE_NOUNS or word in EVENT_NOUNS:
            return word
        if word in {"of", "the", "a", "an", "further", "distinct", "reported", "total",
                    "additional", "separate", "individual", "unique", "such", "other"}:
            continue
        return None
    return None

def verify_section(
    text: str,
    packet: Packet,
    store: FactStore,
    *,
    banned_phrases: Iterable[str] = BANNED_PHRASES,
) -> VerificationResult:
    allowed = build_allowed(packet, store)
    violations: list[Violation] = []
    matched: list[MatchedClaim] = []
    lowered = text.lower()

    for phrase in banned_phrases:
        index = lowered.find(phrase)
        if index >= 0:
            violations.append(
                Violation(
                    kind="banned_phrase",
                    detail=f"states or implies a conclusion this report cannot support: {phrase!r}",
                    excerpt=_excerpt(text, index, index + len(phrase)),
                )
            )

    violations.extend(_check_absences(text, lowered, packet))

    working = text
    for match in _CITATION.finditer(text):
        citation = re.sub(r"\s+", " ", match.group(0)).strip()
        basis = store.get("meta.regulatory_basis").value if "meta.regulatory_basis" in store else ""
        normalised_basis = re.sub(r"\s+", " ", str(basis)).lower()
        if not normalised_basis or citation.lower() not in normalised_basis:
            violations.append(
                Violation(
                    kind="unsupported_citation",
                    detail=f"cites {citation!r}, which is not the regulatory basis configured for this report",
                    excerpt=_excerpt(text, match.start(), match.end()),
                )
            )
    working = _CITATION.sub(lambda m: "#" * len(m.group(0)), working)

    claims_checked = 0
    for match in _ISO_DATE.finditer(working):
        claims_checked += 1
        token = match.group(0)
        owners = allowed.dates.get(token)
        if owners:
            matched.append(MatchedClaim(text=token, fact_ids=tuple(sorted(owners)), case_count=0))
        else:
            violations.append(
                Violation(
                    kind="ungrounded_date",
                    detail=f"date {token} does not appear in the evidence supplied to this section",
                    excerpt=_excerpt(text, match.start(), match.end()),
                )
            )
    working = _ISO_DATE.sub(lambda m: "#" * len(m.group(0)), working)

    for match in _NUMBER.finditer(working):
        raw, percent_marker = match.group(1), match.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        claims_checked += 1

        if percent_marker:
            token = f"{raw}%"
            owners = allowed.percents.get(token.lower())
            if owners:
                matched.append(MatchedClaim(token, tuple(sorted(owners)), 0))
            else:
                violations.append(
                    Violation(
                        kind="ungrounded_number",
                        detail=(
                            f"percentage {token} was not supplied to this section; "
                            "percentages must be stated as given, not derived"
                        ),
                        excerpt=_excerpt(text, match.start(), match.end()),
                    )
                )
            continue

        owners = allowed.numbers.get(value)
        if not owners:
            violations.append(
                Violation(
                    kind="ungrounded_number",
                    detail=f"the figure {raw} does not appear in the evidence supplied to this section",
                    excerpt=_excerpt(text, match.start(), match.end()),
                )
            )
            continue

        cases_behind = max(
            (len(store.get(fact_id).case_ids) for fact_id in owners if fact_id in store),
            default=0,
        )
        matched.append(MatchedClaim(raw, tuple(sorted(owners)), cases_behind))

        grain_violation = _check_grain(text, match.end(), value, raw, allowed)
        if grain_violation:
            violations.append(grain_violation)

    return VerificationResult(
        section_id=packet.section_id,
        claims_checked=claims_checked,
        matched=tuple(matched),
        violations=tuple(violations),
    )

def _check_grain(
    text: str, position: int, value: float, raw: str, allowed: AllowedRenderings
) -> Violation | None:
    grains = allowed.grain_by_number.get(value, set())
    counting_grains = grains & {GRAIN_CASE, GRAIN_EVENT}
    if len(counting_grains) != 1:
        return None

    noun = _following_noun(text, position)
    if noun is None:
        return None

    grain = next(iter(counting_grains))
    if grain == GRAIN_CASE and noun in EVENT_NOUNS:
        return Violation(
            kind="grain_mismatch",
            detail=(
                f"{raw} is a count of cases, but is written as {noun!r}. "
                "One case may report several reactions, so the two are not interchangeable."
            ),
            excerpt=_excerpt(text, position - len(raw), position + 40),
        )
    if grain == GRAIN_EVENT and noun in CASE_NOUNS:
        return Violation(
            kind="grain_mismatch",
            detail=(
                f"{raw} is a count of reaction events, but is written as {noun!r}."
            ),
            excerpt=_excerpt(text, position - len(raw), position + 40),
        )
    return None

_ABSENCE_MARKERS = (
    "not available",
    "not provided",
    "not supplied",
    "could not be",
    "cannot be",
    "was not possible",
    "no prior",
    "unavailable",
)


_ZERO_ASSERTIONS = (
    re.compile(r"\b(?:were|was|is|are|remained|totall?ed)\s+(?:reported\s+as\s+)?zero\b"),
    re.compile(r"\bzero\s+(?:\w+\s+){0,2}(?:cases|reports|reactions|events|actions|changes|figures|counts)\b"),
    re.compile(r"\b(?:were|was|is|are)\s+(?:reported\s+as\s+)?(?:0|none)\b"),
)


_CONTRASTIVE = ("rather than", "instead of", "as opposed to", "avoid")


_ATTACHED_NEGATION = re.compile(r"\b(?:not|never)\s+(?:be\s+)?$")
_CONTRASTIVE_WINDOW = 40


def _check_absences(text: str, lowered: str, packet: Packet) -> list[Violation]:
    violations: list[Violation] = []
    for fact in packet.absent_facts:
        if not any(marker in lowered for marker in _ABSENCE_MARKERS):
            violations.append(
                Violation(
                    kind="missing_absence_statement",
                    detail=(
                        f"{fact.label!r} is {fact.absence_phrase}, but the section does not "
                        "say so. Silence here reads as an absence of findings."
                    ),
                )
            )
            continue

        subject = re.escape(fact.label.split()[0].lower())
        patterns = (
            re.compile(
                rf"\bno {subject}\w*\s+(?:were|was)\s+(?:reported|identified|taken|received|recorded)"
            ),
            *_ZERO_ASSERTIONS,
        )
        for pattern in patterns:
            for found in pattern.finditer(lowered):
                if _is_contrastive(lowered, found.start()):
                    continue
                violations.append(
                    Violation(
                        kind="absence_reported_as_zero",
                        detail=(
                            f"{fact.label!r} was not supplied, so it cannot be reported as none or zero"
                        ),
                        excerpt=_excerpt(text, found.start(), found.end()),
                    )
                )
                break
    return violations

def _is_contrastive(lowered: str, position: int) -> bool:
    window = lowered[max(0, position - _CONTRASTIVE_WINDOW) : position]
    if any(marker in window for marker in _CONTRASTIVE):
        return True
    return bool(_ATTACHED_NEGATION.search(window))

def summarise(results: Iterable[VerificationResult]) -> dict[str, Any]:
    results = list(results)
    checked = sum(result.claims_checked for result in results)
    violations = [violation for result in results for violation in result.violations]
    ungrounded = sum(1 for violation in violations if violation.kind in _UNGROUNDED_KINDS)
    return {
        "sections_verified": len(results),
        "sections_passed": sum(1 for result in results if result.passed),
        "claims_checked": checked,
        "claims_grounded": checked - ungrounded,
        "grounding_score": round((checked - ungrounded) / checked, 4) if checked else 1.0,
        "violations": len(violations),
        "violations_by_kind": {
            kind: sum(1 for violation in violations if violation.kind == kind)
            for kind in sorted({violation.kind for violation in violations})
        },
    }
