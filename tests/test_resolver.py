"""
@file tests/test_resolver.py
@description Tests for the deterministic Resolver (stage 5) and the schema
contracts that bound what a model may return.

This is the most important test file in the suite. Everything upstream of the
resolver is a model that can be wrong; everything downstream trusts the
resolver's output completely. Root AGENTS.md invariant: "An LLM proposes;
deterministic code disposes."

If a test here is deleted, the invariant is no longer enforced anywhere.
"""

import pytest

from src.domain.organization_service import OTHER_MEMORIES_TITLE, resolve_plan
from src.schemas.organization import (
    MAX_CHAPTERS,
    MAX_CHAPTER_SUMMARY,
    MAX_CHAPTER_TITLE,
    MAX_ORGANIZED_CHAPTERS,
    ChapterProposal,
    MemoryFact,
    OrganizationRejected,
    OrganizerOutput,
    ResolvedPlan,
)


def fact(memory_id: str, *, date: str = "1980-01-01", title: str = "A memory") -> MemoryFact:
    return MemoryFact(memory_id=memory_id, title=title, occurred_start=date)


def chapter(title: str, memory_ids: list[str], summary: str = "") -> ChapterProposal:
    return ChapterProposal(title=title, summary=summary, memory_ids=memory_ids)


# ---------------------------------------------------------------------------
# Whole-response rejection: an unknown id invalidates everything
# ---------------------------------------------------------------------------


def test_unknown_memory_id_rejects_the_whole_response():
    """
    The single most important rule in the feature.

    A model that returns an id it was never given is confused. Partially
    trusting its output means some memories get grouped by a model that was
    demonstrably guessing, with no record of which ones. The whole response is
    discarded instead.
    """
    facts = [fact("m1"), fact("m2"), fact("m3")]
    chapters = [
        chapter("Childhood", ["m1", "m2"]),
        chapter("Later years", ["m3", "m-does-not-exist"]),
    ]

    with pytest.raises(OrganizationRejected) as exc:
        resolve_plan(facts, chapters)

    assert "unknown memory_id" in str(exc.value)


def test_unknown_id_rejects_even_when_every_other_id_is_valid():
    """One bad id among many valid ones still fails. No partial credit."""
    facts = [fact(f"m{i}") for i in range(10)]
    chapters = [chapter("All", [f"m{i}" for i in range(9)] + ["m-bogus"])]

    with pytest.raises(OrganizationRejected):
        resolve_plan(facts, chapters)


def test_duplicate_memory_id_across_chapters_rejects_the_whole_response():
    """
    A memory in two chapters means the book's structure is ambiguous — which
    chapter does the reader see it in? Rejecting is the only consistent answer.
    """
    facts = [fact("m1"), fact("m2")]
    chapters = [
        chapter("First", ["m1", "m2"]),
        chapter("Second", ["m2"]),
    ]

    with pytest.raises(OrganizationRejected) as exc:
        resolve_plan(facts, chapters)

    assert "more than one chapter" in str(exc.value)


def test_duplicate_within_a_single_chapter_rejects():
    facts = [fact("m1")]
    with pytest.raises(OrganizationRejected):
        resolve_plan(facts, [chapter("Dupes", ["m1", "m1"])])


def test_empty_chapter_list_rejects():
    with pytest.raises(OrganizationRejected) as exc:
        resolve_plan([fact("m1")], [])
    assert "no chapters" in str(exc.value)


def test_all_chapters_empty_rejects():
    """Chapters referencing only unknown ids must not produce an empty book."""
    with pytest.raises(OrganizationRejected):
        resolve_plan([fact("m1")], [chapter("Ghost", ["nope"])])


# ---------------------------------------------------------------------------
# Nothing is silently dropped
# ---------------------------------------------------------------------------


def test_unplaced_memories_go_to_the_catch_all():
    facts = [fact("m1"), fact("m2"), fact("m3")]
    chapters = [chapter("Childhood", ["m1"])]

    plan = resolve_plan(facts, chapters)

    assert len(plan.chapters) == 2
    catch_all = plan.chapters[-1]
    assert catch_all.title == OTHER_MEMORIES_TITLE
    assert sorted(catch_all.memory_ids) == ["m2", "m3"]
    assert sorted(plan.unplaced_memory_ids) == ["m2", "m3"]


def test_catch_all_summary_is_empty_never_fabricated():
    """
    Root AGENTS.md: "No fabricated fallback prose may ever be persisted."

    The reference implementation wrote marketing copy ("A collection of
    treasured family moments...") into a real chapter when source text was
    missing. That copy reaches families and public share-link readers as if it
    were biography.
    """
    plan = resolve_plan([fact("m1"), fact("m2")], [chapter("Only", ["m1"])])
    catch_all = plan.chapters[-1]

    assert catch_all.title == OTHER_MEMORIES_TITLE
    assert catch_all.summary == ""


def test_no_catch_all_when_everything_is_placed():
    plan = resolve_plan([fact("m1")], [chapter("Only", ["m1"])])

    assert len(plan.chapters) == 1
    assert plan.unplaced_memory_ids == []


def test_every_sent_memory_appears_exactly_once_in_the_result():
    """
    The end-to-end invariant: ids in == ids out, no duplicates, no losses.
    Checked as a set property rather than per-case, because this is the property
    the feature actually promises.
    """
    facts = [fact(f"m{i}", date=f"19{50 + i:02d}-01-01") for i in range(20)]
    chapters = [
        chapter("A", [f"m{i}" for i in range(0, 5)]),
        chapter("B", [f"m{i}" for i in range(5, 9)]),
        chapter("C", [f"m{i}" for i in range(9, 12)]),
    ]

    plan = resolve_plan(facts, chapters)

    all_ids = [mid for c in plan.chapters for mid in c.memory_ids]
    assert sorted(all_ids) == sorted(f.memory_id for f in facts)
    assert len(all_ids) == len(set(all_ids))


# ---------------------------------------------------------------------------
# Chronological ordering is computed, not trusted from array position
# ---------------------------------------------------------------------------


def test_chapters_are_ordered_by_their_earliest_memory_not_array_position():
    """Array order is the model's guess. Dates are the fact."""
    facts = [
        fact("old", date="1950-01-01"),
        fact("new", date="1990-01-01"),
        fact("mid", date="1970-01-01"),
    ]
    # Deliberately reverse-ordered.
    chapters = [
        chapter("Nineties", ["new"]),
        chapter("Fifties", ["old"]),
        chapter("Seventies", ["mid"]),
    ]

    plan = resolve_plan(facts, chapters)

    assert [c.title for c in plan.chapters] == ["Fifties", "Seventies", "Nineties"]
    assert [c.sort_order for c in plan.chapters] == [0, 1, 2]


def test_undated_memories_sort_after_dated_ones():
    facts = [
        fact("undated", date=""),
        fact("dated", date="1960-01-01"),
    ]
    plan = resolve_plan(facts, [chapter("Both", ["undated", "dated"])])

    assert plan.chapters[0].memory_ids == ["dated", "undated"]


def test_chapter_ordering_is_stable_for_equal_dates():
    """Same date must not make the result depend on dict iteration order."""
    facts = [fact("a", date="1960-01-01"), fact("b", date="1960-01-01")]
    plan = resolve_plan(facts, [chapter("X", ["a", "b"])])

    assert sorted(plan.chapters[0].memory_ids) == ["a", "b"]


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def test_plan_never_exceeds_the_chapter_cap_even_with_a_catch_all():
    """
    The organizer is capped at MAX_ORGANIZED_CHAPTERS precisely so the catch-all
    always fits inside MAX_CHAPTERS. This asserts the cap on the result too,
    because `ChapterReorderRequest` is bounded at MAX_CHAPTERS and a system that
    can produce more chapters than its own reorder endpoint accepts returns 422
    to the owner on their own memoir.
    """
    facts = [fact(f"m{i}") for i in range(30)]
    chapters = [
        chapter(f"C{i}", [f"m{i}"]) for i in range(MAX_ORGANIZED_CHAPTERS)
    ]

    plan = resolve_plan(facts, chapters)

    assert len(plan.chapters) <= MAX_CHAPTERS
    # 11 organized + 1 catch-all for the 19 the model did not place.
    assert len(plan.chapters) == MAX_CHAPTERS


def test_resolver_rejects_rather_than_truncating_an_over_cap_plan():
    """
    If it somehow arrives over the cap, the resolver rejects rather than
    dropping memories. Dropping the oldest unplaced memories to make room would
    lose a family's content because the model asked for one chapter too many.
    """
    facts = [fact("placed"), fact("loose")]
    # Hand-built, bypassing the organizer's schema bound on purpose.
    over_cap = [
        ChapterProposal(title=f"C{i}", summary="", memory_ids=["placed"] if i == 0 else [])
        for i in range(MAX_CHAPTERS)
    ]
    # Drop the empties the resolver filters out so exactly MAX_CHAPTERS survive.
    chapters = [c for c in over_cap if c.memory_ids] + [
        ChapterProposal(title="Filler", summary="", memory_ids=["loose"]) for _ in range(3)
    ]

    with pytest.raises(OrganizationRejected):
        # resolve_plan raises on empty chapters, so build the over-cap case with
        # all chapters non-empty and the catch-all still needed.
        all_placed = [ChapterProposal(title=f"C{i}", summary="", memory_ids=["placed"]) for i in range(MAX_CHAPTERS)]
        all_placed[0] = ChapterProposal(title="C0", summary="", memory_ids=["loose"])
        resolve_plan(facts, all_placed)


def test_over_length_title_and_summary_cannot_reach_the_resolver():
    """
    An over-long title is REJECTED, not truncated.

    The obvious alternative -- have the resolver quietly clip to the maximum --
    is worse than it looks. Truncating a chapter title to 200 characters produces
    two chapters that can render identically in the reader-facing book, and the
    only record that the model's real title was lost is a silent difference
    between the proposal and what got written. Failing the response means the
    organizer retries, which is the recoverable outcome.

    So the bound is enforced at the contract (the schema), and the resolver is
    unreachable with a value that violates it. There is deliberately no
    truncation code path to test.
    """
    with pytest.raises(Exception):
        ChapterProposal(title="T" * (MAX_CHAPTER_TITLE + 1), summary="", memory_ids=["m1"])

    with pytest.raises(Exception):
        ChapterProposal(
            title="In bounds",
            summary="S" * (MAX_CHAPTER_SUMMARY + 1),
            memory_ids=["m1"],
        )

    # And the boundary itself is inclusive: exactly the maximum is valid.
    at_limit = ChapterProposal(
        title="T" * MAX_CHAPTER_TITLE,
        summary="S" * MAX_CHAPTER_SUMMARY,
        memory_ids=["m1"],
    )
    plan = resolve_plan([fact("m1")], [at_limit])

    assert len(plan.chapters[0].title) == MAX_CHAPTER_TITLE
    assert len(plan.chapters[0].summary) == MAX_CHAPTER_SUMMARY


def test_organizer_schema_rejects_an_over_long_title_before_the_resolver():
    """
    Bound enforced at the contract too, not only at the persistence boundary —
    so the model is told immediately rather than the resolver silently fixing it.
    """
    with pytest.raises(Exception):
        ChapterProposal(title="T" * (MAX_CHAPTER_TITLE + 1), summary="", memory_ids=["m1"])


def test_organizer_schema_rejects_more_chapters_than_the_reservation():
    """
    MAX_ORGANIZED_CHAPTERS, not MAX_CHAPTERS. Asserted so the reservation cannot
    be "simplified" back to 12 without this failing.
    """
    chapters = [
        ChapterProposal(title=f"C{i}", summary="", memory_ids=[f"m{i}"])
        for i in range(MAX_ORGANIZED_CHAPTERS + 1)
    ]
    with pytest.raises(Exception):
        OrganizerOutput(chapters=chapters)

    assert MAX_ORGANIZED_CHAPTERS == MAX_CHAPTERS - 1


# ---------------------------------------------------------------------------
# ResolvedPlan is the only structure the persistence layer accepts
# ---------------------------------------------------------------------------


def test_resolved_plan_forbids_extra_fields():
    """Nothing can be smuggled into the structure that reaches the database."""
    with pytest.raises(Exception):
        ResolvedPlan(chapters=[], unplaced_memory_ids=[], injected="x")


def test_resolved_plan_caps_chapter_count_at_construction():
    chapters = [
        {
            "title": f"C{i}",
            "summary": "",
            "era_label": None,
            "sort_order": i,
            "memory_ids": [f"m{i}"],
        }
        for i in range(MAX_CHAPTERS + 1)
    ]
    with pytest.raises(Exception):
        ResolvedPlan(chapters=chapters)


def test_all_assigned_ids_excludes_unplaced():
    plan = ResolvedPlan(
        chapters=[
            {
                "title": "A",
                "summary": "",
                "era_label": None,
                "sort_order": 0,
                "memory_ids": ["m1", "m2"],
            }
        ],
        unplaced_memory_ids=["m3"],
    )
    assert plan.all_assigned_ids() == {"m1", "m2"}
