import pytest

from agentloop.references import ScopedReferences


def test_references_are_stable_and_resolve_without_rewriting_values():
    store = ScopedReferences()
    first = store.add("job-a", "request", "full-long-value-1234", "case-1")
    again = store.add("job-a", "request", "full-long-value-1234", "query-2")
    assert again == first
    assert store.resolve("job-a", first.id).value == "full-long-value-1234"
    assert store.find("job-a", "full-long-value") is None
    assert first.source == "case-1"


def test_reference_cannot_cross_scope_or_kind():
    store = ScopedReferences()
    ref = store.add("job-a", "request", "value-12345678", "case-1")
    with pytest.raises(ValueError, match="scope"):
        store.resolve("job-b", ref.id)
    with pytest.raises(ValueError, match="kind"):
        store.resolve("job-a", ref.id, {"resource"})
    with pytest.raises(ValueError, match="scope"):
        store.add("job-b", "request", "child-12345678", "query-1", parent=ref.id)


def test_child_keeps_origin_and_shared_values_keep_explicit_owners():
    store = ScopedReferences()
    parent = store.add("job-a", "request", "request-1234", "case-1")
    child = store.add("job-a", "resource", "resource-2345", "query-1", parent=parent.id)
    store.add("job-b", "resource", child.value, "case-2")
    assert child.parent == parent.id
    assert store.owners("resource", child.value) == {"job-a", "job-b"}
    assert store.for_scope("job-a")[1]["source"] == "query-1"


def test_reference_capacity_does_not_silently_evict_valid_handles():
    store = ScopedReferences(limit=1)
    ref = store.add("a", "request", "value", "source")
    with pytest.raises(ValueError, match="limit"):
        store.add("a", "request", "another", "source")
    assert store.resolve("a", ref.id) == ref
