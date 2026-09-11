"""Stable references to caller-verified values, scoped to a task or entity.

The caller establishes provenance and decides which observations may introduce
new values. This store never discovers identities by searching arbitrary text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Reference:
    id: str
    scope: str
    kind: str
    value: str
    source: str
    parent: str | None = None


class ScopedReferences:
    def __init__(self, limit: int = 1024):
        self.limit = limit
        self.records: dict[str, Reference] = {}
        self._values: dict[tuple[str, str, str], str] = {}
        self.catalog: list[dict] = []

    def add(self, scope, kind, value, source, parent=None) -> Reference:
        if not all(isinstance(x, str) and x for x in (scope, kind, value, source)):
            raise ValueError("reference scope, kind, value and source are required")
        if parent is not None:
            self.resolve(scope, parent)
        key = (scope, kind, value)
        if key in self._values:
            return self.records[self._values[key]]
        if len(self.records) >= self.limit:
            raise ValueError("reference catalog limit reached")
        ref = Reference(f"I{len(self.records) + 1}", scope, kind, value, source, parent)
        self.records[ref.id] = ref
        self._values[key] = ref.id
        self.catalog.append(asdict(ref))
        return ref

    def resolve(self, scope, ref_id, kinds=None) -> Reference:
        ref = self.records.get(ref_id)
        if ref is None or ref.scope != scope:
            raise ValueError("reference does not belong to this scope")
        if kinds is not None and ref.kind not in kinds:
            raise ValueError("reference kind is not permitted for this operation")
        return ref

    def find(self, scope, value, kinds=None) -> Reference | None:
        return next(
            (
                r
                for r in self.records.values()
                if r.scope == scope
                and r.value == value
                and (kinds is None or r.kind in kinds)
            ),
            None,
        )

    def owners(self, kind, value) -> set[str]:
        return {
            r.scope
            for r in self.records.values()
            if r.kind == kind and r.value == value
        }

    def for_scope(self, scope) -> list[dict]:
        return [dict(row) for row in self.catalog if row["scope"] == scope]
