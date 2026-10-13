"""
@file tests/test_chat_proposals.py
@description Tests for the archive chat's proposal validation.

The chat agent returns structural changes for the owner to approve. Those
changes name chapter and memory ids, and the model supplying them is an LLM,
so every id is checked against the memoir's real contents before the owner is
ever shown something they can click "accept" on.

That check is what these tests cover. It is not the security boundary -- the
apply path re-verifies ownership on every write regardless -- but it is what
stops a hallucinated id becoming a proposal that silently does nothing, and
what stops an id belonging to a different memoir being offered at all.

No database and no provider: the LLM call and the archive lookups are
replaced with fakes, so the assertions are purely about which proposals
survive validation.
"""

import pytest

from src.domain import organization_service as svc
from src.schemas.organization import ChatAgentOutput, ChatProposedAction

CHAPTER_A = "11111111-1111-1111-1111-111111111111"
CHAPTER_B = "22222222-2222-2222-2222-222222222222"
MEMORY_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FOREIGN_CHAPTER = "99999999-9999-9999-9999-999999999999"
FOREIGN_MEMORY = "88888888-8888-8888-8888-888888888888"


@pytest.fixture
def archive(monkeypatch):
    """A memoir with two chapters and one memory, and no rate limiting."""
    monkeypatch.setattr(svc, "fetch_archive_context", lambda _mid: "== CONTENTS ==")
    monkeypatch.setattr(
        svc,
        "fetch_archive_index",
        lambda _mid: {
            "chapters": [
                {"id": CHAPTER_A, "title": "The little things", "sort_order": 0},
                {"id": CHAPTER_B, "title": "By the sea", "sort_order": 1},
            ],
            "memories": [{"id": MEMORY_A, "title": "Sunday mornings", "chapter_id": CHAPTER_A}],
        },
    )

    async def no_rate_limit(_key):
        return None

    monkeypatch.setattr(svc, "_check_chat_rate_limit", no_rate_limit)


def stub_agent(monkeypatch, actions):
    """Replaces the provider call with a fixed ChatAgentOutput."""

    async def fake_complete_json(**_kwargs):
        return ChatAgentOutput(reply="Here is what I'd do.", proposed_actions=actions)

    monkeypatch.setattr(svc, "complete_json", fake_complete_json)


async def run_chat():
    return await svc.chat_with_archive("memoir-1", "user-1", "tidy this up", [])


@pytest.mark.asyncio
async def test_valid_move_survives(archive, monkeypatch):
    stub_agent(
        monkeypatch,
        [
            ChatProposedAction(
                action_type="move_memory",
                target_memory_ids=[MEMORY_A],
                target_chapter_id=CHAPTER_B,
                rationale="It belongs with the seaside memories.",
            )
        ],
    )
    reply, actions = await run_chat()
    assert reply == "Here is what I'd do."
    assert len(actions) == 1
    assert actions[0].action_type == "move_memory"
    assert actions[0].target_memory_ids == [MEMORY_A]
    assert actions[0].target_chapter_id == CHAPTER_B


@pytest.mark.asyncio
async def test_unknown_chapter_id_is_dropped(archive, monkeypatch):
    """An id that isn't in this memoir never reaches the owner."""
    stub_agent(
        monkeypatch,
        [
            ChatProposedAction(
                action_type="move_memory",
                target_memory_ids=[MEMORY_A],
                target_chapter_id=FOREIGN_CHAPTER,
            )
        ],
    )
    _reply, actions = await run_chat()
    assert actions == []


@pytest.mark.asyncio
async def test_unknown_memory_id_is_dropped(archive, monkeypatch):
    stub_agent(
        monkeypatch,
        [
            ChatProposedAction(
                action_type="move_memory",
                target_memory_ids=[FOREIGN_MEMORY],
                target_chapter_id=CHAPTER_B,
            )
        ],
    )
    _reply, actions = await run_chat()
    assert actions == []


@pytest.mark.asyncio
async def test_partially_valid_memory_list_is_dropped_whole(archive, monkeypatch):
    """
    Half a move is worse than none: applying only the recognised ids would
    carry out something different from what the owner read and approved.
    """
    stub_agent(
        monkeypatch,
        [
            ChatProposedAction(
                action_type="merge_into_chapter",
                target_memory_ids=[MEMORY_A, FOREIGN_MEMORY],
                target_chapter_id=CHAPTER_B,
            )
        ],
    )
    _reply, actions = await run_chat()
    assert actions == []


@pytest.mark.asyncio
async def test_rename_without_a_new_title_is_dropped(archive, monkeypatch):
    """A proposal with nothing to apply would be an approvable no-op."""
    stub_agent(
        monkeypatch,
        [
            ChatProposedAction(
                action_type="rename_chapter",
                target_chapter_id=CHAPTER_A,
                preview_before="The little things",
                preview_after="   ",
            )
        ],
    )
    _reply, actions = await run_chat()
    assert actions == []


@pytest.mark.asyncio
async def test_rename_with_a_new_title_survives(archive, monkeypatch):
    stub_agent(
        monkeypatch,
        [
            ChatProposedAction(
                action_type="rename_chapter",
                target_chapter_id=CHAPTER_A,
                preview_before="The little things",
                preview_after="Little things, remembered",
            )
        ],
    )
    _reply, actions = await run_chat()
    assert len(actions) == 1
    assert actions[0].preview_after == "Little things, remembered"


@pytest.mark.asyncio
async def test_conversational_reply_with_no_actions(archive, monkeypatch):
    """Asking a question must not manufacture a change to approve."""
    stub_agent(monkeypatch, [])
    reply, actions = await run_chat()
    assert reply
    assert actions == []
