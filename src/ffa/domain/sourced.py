"""Values that remember where they came from.

This module is the heart of the project. Everything else — the ESPN adapter,
the manual REPL, the simulator — exists to produce `Sourced` values that
`merge` combines.

The design problem it solves: on draft day, different facts about the same
pick arrive from different places at different times and with different
reliability. ESPN may report that a player sold without reporting the price.
You may type the price by hand a minute later. A later poll may re-report the
same pick. None of that can be modelled as "API mode" or "manual mode" — it
has to be per-field, or the partial-fallback case becomes a special case that
gets bolted on and quietly breaks.

So every observed attribute is a `Sourced[T]`, and one function decides who
wins.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Generic, TypeVar

from ffa.domain.enums import Provenance
from ffa.util.clock import from_iso, to_iso

T = TypeVar("T")


@dataclass(frozen=True)
class Sourced(Generic[T]):
    """One observation of one field.

    `value=None` is legal and *meaningful*. It says "we know a sale happened
    and we do not know the price" — a fact, not an absence. That distinction is
    load-bearing: the price field is present with an explicit null, so it can
    later be filled in by merge rather than looking like a schema gap.
    """

    value: T | None
    provenance: Provenance
    observed_at: datetime
    confidence: float = 1.0
    note: str | None = None

    @property
    def is_known(self) -> bool:
        return self.value is not None

    # --- serialization ---------------------------------------------------
    #
    # Verbose on purpose. A 300-line journal's size is irrelevant; a line you
    # can read at 2am mid-draft is not. `confidence` and `note` are omitted at
    # their defaults so the common line stays short, and a missing key means
    # "default" — which is free forward-compatibility when fields are added.

    def to_obj(self) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "value": self.value,
            "provenance": self.provenance.value,
            "observed_at": to_iso(self.observed_at),
        }
        if self.confidence != 1.0:
            obj["confidence"] = self.confidence
        if self.note is not None:
            obj["note"] = self.note
        return obj

    @classmethod
    def from_obj(
        cls, obj: dict[str, Any], decode: Callable[[Any], T] | None = None
    ) -> "Sourced[T]":
        raw = obj.get("value")
        # Only decode real values. Running a decoder over None would turn
        # "price unknown" into a crash or a bogus zero.
        value = decode(raw) if (decode is not None and raw is not None) else raw
        return cls(
            value=value,
            provenance=Provenance.parse(obj.get("provenance", "UNKNOWN")),
            observed_at=from_iso(obj["observed_at"]),
            confidence=float(obj.get("confidence", 1.0)),
            note=obj.get("note"),
        )


def known(
    value: T,
    provenance: Provenance,
    observed_at: datetime,
    *,
    confidence: float = 1.0,
    note: str | None = None,
) -> Sourced[T]:
    return Sourced(value, provenance, observed_at, confidence, note)


def unknown(observed_at: datetime, *, note: str | None = None) -> Sourced[Any]:
    """An explicit "we don't know this yet".

    Tagged `UNKNOWN` with zero confidence rather than inheriting the reporter's
    provenance. That matters: if a manual "price unknown" entry carried
    `MANUAL`, it would outrank a later real ESPN price under any rule that
    consults provenance first.
    """
    return Sourced(None, Provenance.UNKNOWN, observed_at, confidence=0.0, note=note)


def merge(existing: Sourced[T] | None, incoming: Sourced[T]) -> Sourced[T]:
    """Combine two observations of the same field. Pure; never mutates.

    The order of these rules is the whole design, so spelling it out:

    1-2. **Knowledge beats ignorance, regardless of provenance.** A known value
         always beats an unknown one, in both directions. This pair must be
         checked *above* provenance, and rule 2 is the one that is easy to
         forget and expensive to omit: without it, typing `sold mahomes ? t3`
         (MANUAL, price unknown) would make the later real ESPN price
         (ESPN_API) lose on precedence — permanently rejecting the correct
         number. That inverts the feature this whole module exists to provide.

    3-4. Both values known: higher provenance wins. MANUAL outranks ESPN_API so
         a human correction is never stomped by a subsequent poll.

    5.   Same provenance: newer wins. The `>=` is deliberate — on an exact
         timestamp tie the *incoming* value wins, which means journal order is
         the final tiebreaker. That is what keeps replay deterministic when
         tests freeze the clock and every event shares a timestamp.

    Idempotent by construction: merging the same observation twice returns an
    equal value, which is what makes a double-poll harmless.
    """
    if existing is None:
        return incoming

    # 1-2. Knowledge beats ignorance.
    if incoming.value is None and existing.value is not None:
        return existing
    if existing.value is None and incoming.value is not None:
        return incoming

    # 3-4. Both known (or both unknown): rank decides.
    if incoming.provenance.rank > existing.provenance.rank:
        return incoming
    if incoming.provenance.rank < existing.provenance.rank:
        return existing

    # 5. Equal rank: newer wins, ties go to journal order.
    return incoming if incoming.observed_at >= existing.observed_at else existing


def with_value(source: Sourced[T], value: T | None) -> Sourced[T]:
    """Copy a `Sourced` with a different value, keeping its metadata."""
    return replace(source, value=value)
