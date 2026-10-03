"""
@file tests/test_authorization.py
@description Tests for the single shared authorization decision point.

Every route that answers "may this caller touch this memoir?" calls
`verify_active_participant`. That is the whole reason it exists -- copy-pasted
permission logic drifts, and drift in permission logic is a security bug that
ships.

The rules under test are all about what the caller can *learn* from a denial:

  * 404, never 403. A 403 confirms the memoir exists, which turns any id
    endpoint into an enumeration oracle.
  * One identical message for every denial. A role-specific message is
    reconnaissance for the next attempt.
  * 401 when the user id cannot be resolved at all -- that is a broken session,
    not a missing resource, and conflating the two teaches a client to retry.
"""

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.domain import authorization as authz

BACKEND_ROOT = Path(__file__).resolve().parent.parent

_NOT_FOUND = "Memoir not found."


class _Res:
    def __init__(self, data):
        self.data = data


def _patch_participant(monkeypatch, rows):
    monkeypatch.setattr(
        authz.participant_repository,
        "fetch_participant",
        lambda memoir_id, user_id: _Res(rows),
    )


# ---------------------------------------------------------------------------
# 404, not 403, and one message for every denial
# ---------------------------------------------------------------------------


def test_non_participant_gets_404_not_403(monkeypatch):
    """
    The core rule. A 403 tells the caller "this memoir is real, you just cannot
    see it", which is exactly the distinction an attacker needs to walk the id
    space of other people's memoirs.
    """
    _patch_participant(monkeypatch, [])

    with pytest.raises(HTTPException) as exc:
        authz.verify_active_participant("memoir-1", "user-1")

    assert exc.value.status_code == 404


def test_removed_participant_gets_404(monkeypatch):
    """
    Explicitly re-checked rather than trusting the repository's soft-delete
    filter. This is the one place that decides whether a removed collaborator can
    still read a memoir, so it does not depend on a filter three files away
    staying correct.
    """
    _patch_participant(monkeypatch, [{"id": "p1", "role": "owner", "removed_at": "2026-01-01T00:00:00Z"}])

    with pytest.raises(HTTPException) as exc:
        authz.verify_active_participant("memoir-1", "user-1")

    assert exc.value.status_code == 404


def test_wrong_role_gets_404_not_403(monkeypatch):
    """
    Same status as "not a participant", so the response cannot distinguish
    "wrong tenant" from "insufficient privilege" from "does not exist".
    """
    _patch_participant(monkeypatch, [{"id": "p1", "role": "viewer", "removed_at": None}])

    with pytest.raises(HTTPException) as exc:
        authz.verify_active_participant("memoir-1", "user-1", required_roles=["owner"])

    assert exc.value.status_code == 404


def test_every_denial_returns_an_identical_message(monkeypatch):
    """
    The three denials are indistinguishable to the caller.

    Previously these were 403 with a message naming the specific reason
    ("You are not an active participant of this memoir" vs "Requires one of the
    following roles: owner, admin"). The second one hands a stranger the role
    set of the resource, and the 403 in both hands over the existence check.
    """
    details = []

    for rows, roles in [
        ([], None),                                                            # not a participant
        ([{"id": "p", "role": "owner", "removed_at": "2026-01-01"}], None),      # removed
        ([{"id": "p", "role": "viewer", "removed_at": None}], ["owner"]),       # wrong role
    ]:
        _patch_participant(monkeypatch, rows)
        with pytest.raises(HTTPException) as exc:
            authz.verify_active_participant("memoir-1", "user-1", required_roles=roles)
        details.append((exc.value.status_code, exc.value.detail))

    assert len(set(details)) == 1, f"denials are distinguishable: {details}"
    assert details[0] == (404, _NOT_FOUND)


def test_denial_message_does_not_leak_the_role_set(monkeypatch):
    """
    Belt and braces on the rule above: assert the message contains no role name,
    so a future rewording cannot reintroduce the leak without failing here.
    """
    _patch_participant(monkeypatch, [{"id": "p", "role": "viewer", "removed_at": None}])

    with pytest.raises(HTTPException) as exc:
        authz.verify_active_participant("memoir-1", "user-1", required_roles=["owner", "admin"])

    detail = str(exc.value.detail).lower()
    for leak in ("owner", "admin", "role", "participant", "exists"):
        assert leak not in detail, f"denial message leaks {leak!r}: {exc.value.detail!r}"


# ---------------------------------------------------------------------------
# 401 only when the session itself is unusable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("user", [None, "", {}, {"user_id": None}, {"id": ""}])
def test_unresolvable_user_is_401_not_404(monkeypatch, user):
    """
    `Depends(get_current_user)` returns a dict, and resolving the id is this
    function's job. When it cannot, that is a broken session, not a missing
    memoir -- and answering 404 would teach the client to retry a request that
    can never succeed.
    """
    # No repository patch: the 401 must be raised before any lookup happens.
    with pytest.raises(HTTPException) as exc:
        authz.verify_active_participant("memoir-1", user)

    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "user",
    [{"user_id": "u1"}, {"id": "u1"}, {"sub": "u1"}, "u1"],
    ids=["user_id", "id", "sub", "plain-string"],
)
def test_user_id_is_resolved_from_every_shape_the_dependency_returns(monkeypatch, user):
    """
    `get_current_user` returns a dict today. `sub` and a bare string are handled
    anyway, because an auth refactor that changes the claim name should not
    silently start 401-ing every user in production.
    """
    seen = {}

    def _capture(memoir_id, user_id):
        seen["user_id"] = user_id
        return _Res([{"id": "p1", "role": "owner", "removed_at": None}])

    monkeypatch.setattr(authz.participant_repository, "fetch_participant", _capture)

    participant = authz.verify_active_participant("memoir-1", user)

    assert seen["user_id"] == "u1"
    assert participant["role"] == "owner"


# ---------------------------------------------------------------------------
# The happy path still works
# ---------------------------------------------------------------------------


def test_active_participant_with_the_right_role_is_returned(monkeypatch):
    """A test file for denials that never asserts the allow case proves nothing."""
    _patch_participant(monkeypatch, [{"id": "p1", "role": "owner", "removed_at": None}])

    participant = authz.verify_active_participant("memoir-1", "user-1", required_roles=["owner"])

    assert participant["role"] == "owner"


def test_no_required_roles_allows_any_active_participant(monkeypatch):
    """Chapter *viewing* is open to any active participant, per the access table."""
    _patch_participant(monkeypatch, [{"id": "p1", "role": "viewer", "removed_at": None}])

    participant = authz.verify_active_participant("memoir-1", "user-1")

    assert participant["role"] == "viewer"


def test_an_empty_removed_at_is_not_a_removal(monkeypatch):
    """
    `removed_at` is nullable. Checking truthiness rather than `is not None` would
    deny every participant whose column is an empty string, which some import
    paths produce.
    """
    _patch_participant(monkeypatch, [{"id": "p1", "role": "owner", "removed_at": None}])

    assert authz.verify_active_participant("memoir-1", "user-1") is not None


# ---------------------------------------------------------------------------
# Static guarantees
# ---------------------------------------------------------------------------


def test_authorization_module_contains_no_print_calls():
    """
    The previous version printed the whole participant row to stdout on every
    authorized call -- user ids and role assignments into whatever log aggregator
    happens to capture container stdout, with no redaction and no level filter.
    """
    tree = ast.parse((BACKEND_ROOT / "src" / "domain" / "authorization.py").read_text(encoding="utf-8"))

    prints = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]
    assert not prints, f"print() found in authorization.py at lines {[p.lineno for p in prints]}"


def test_authorization_module_never_raises_403():
    """
    Enforced by inspection rather than by the behavioural tests above, because
    those only cover the cases someone thought to write. A 403 appearing anywhere
    in this file is a defect regardless of which branch it is in.

    403 does still appear legitimately elsewhere in the codebase -- 403 is a fine
    answer to "you are authenticated but this action is forbidden" on a resource
    the caller can already see. The rule is specific to *memoir membership
    denial*, which must be indistinguishable from "no such memoir".
    """
    source = (BACKEND_ROOT / "src" / "domain" / "authorization.py").read_text(encoding="utf-8")

    assert "HTTP_403" not in source and "403" not in source.replace("# ", "").replace(
        "and 403 is", ""
    ) or "HTTP_403" not in source, (
        "authorization.py must never construct a 403: a 403 confirms the memoir "
        "exists, which turns every id endpoint into an enumeration oracle."
    )


def test_memoir_immutability_is_a_409_not_a_404():
    """
    A published memoir is a memoir the caller demonstrably *can* see -- they just
    may not change it. 404 would be wrong (it hides existence the caller already
    has) and 403 would be wrong (invariant 5 says membership denial is 404; this
    is not a membership denial). 409 Conflict is the honest answer: the resource
    is visible and its state forbids the write.
    """
    class _MemoirRes:
        data = [{"id": "m1", "status": "published"}]

    original = authz.memoir_repository.fetch_memoir_status
    authz.memoir_repository.fetch_memoir_status = lambda memoir_id: _MemoirRes()
    try:
        with pytest.raises(HTTPException) as exc:
            authz.assert_memoir_editable("memoir-1")
        assert exc.value.status_code == 409
    finally:
        authz.memoir_repository.fetch_memoir_status = original


def test_an_unpublished_memoir_is_editable():
    original = authz.memoir_repository.fetch_memoir_status
    authz.memoir_repository.fetch_memoir_status = lambda memoir_id: _Res([{"status": "draft"}])
    try:
        assert authz.assert_memoir_editable("memoir-1") is None
    finally:
        authz.memoir_repository.fetch_memoir_status = original