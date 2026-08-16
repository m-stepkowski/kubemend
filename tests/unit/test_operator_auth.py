"""Webhook bearer-token auth (docs/knowledge/operator-design.md).

`is_authorized` is checked before any scope/cooldown logic runs — see
webhook.py's do_POST — so this is worth testing in isolation from the HTTP
server plumbing.
"""

from __future__ import annotations

from kubemend.operator.webhook import is_authorized

TOKEN = "s3cret-token"


def test_correct_bearer_token_is_authorized() -> None:
    assert is_authorized(f"Bearer {TOKEN}", TOKEN) is True


def test_wrong_bearer_token_is_rejected() -> None:
    assert is_authorized("Bearer wrong-token", TOKEN) is False


def test_missing_header_is_rejected() -> None:
    assert is_authorized(None, TOKEN) is False


def test_header_without_bearer_prefix_is_rejected() -> None:
    assert is_authorized(TOKEN, TOKEN) is False


def test_empty_header_is_rejected() -> None:
    assert is_authorized("", TOKEN) is False


def test_bearer_prefix_with_no_token_is_rejected() -> None:
    assert is_authorized("Bearer ", TOKEN) is False


def test_case_sensitive_prefix_is_required() -> None:
    """`bearer` (lowercase) is not `Bearer` — no case-insensitive matching,
    same discipline as not trying to be clever about the auth header."""
    assert is_authorized(f"bearer {TOKEN}", TOKEN) is False
