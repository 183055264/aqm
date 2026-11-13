# -*- coding: utf-8 -*-
"""ACP protocol details that are easy to get subtly wrong."""

from __future__ import annotations

import threading

import pytest

from aqm.runtime.meeting_session import _ACPMeetingSession
from aqm.runtime.stream_guard import ThinkingGuard


def _bare_session():
    session = object.__new__(_ACPMeetingSession)
    session._turn_lock = threading.Lock()
    session._output_parts = []
    session._thinking_parts = []
    session._thinking_guard = ThinkingGuard()
    session._action_lines = []
    session._narration_parts = []
    session._segment_parts = []
    session._seen_tools = set()
    session._failed_tools = set()
    session.last_thinking = ""
    session.last_actions = ""
    session.system_prompt = ""
    session.session_id = "ses_test"
    session._output_callback = None
    session._thinking_callback = None
    session._action_callback = None
    session._process_callback = None
    return session


def test_permission_response_uses_nested_outcome():
    """ACP expects {"outcome": {"outcome": "selected", "optionId": ...}}.

    Returning the inner object makes the agent treat the decision as an
    unknown outcome ("user rejected permission") and abort the turn, which
    shows up as a member stopping halfway through a script.
    """

    session = _bare_session()
    result = session._handle_client_method(
        "session/request_permission",
        {
            "sessionId": "ses_test",
            "options": [
                {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
                {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
            ],
        },
    )

    assert result == {
        "outcome": {"outcome": "selected", "optionId": "once"}
    }


def test_permission_response_falls_back_to_allow_always():
    session = _bare_session()
    result = session._handle_client_method(
        "session/request_permission",
        {
            "options": [
                {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
            ]
        },
    )

    assert result == {
        "outcome": {"outcome": "selected", "optionId": "always"}
    }


def test_permission_response_cancelled_without_allow_option():
    session = _bare_session()
    result = session._handle_client_method(
        "session/request_permission",
        {
            "options": [
                {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
            ]
        },
    )

    assert result == {"outcome": {"outcome": "cancelled"}}


class _FakeRpc:
    def __init__(self, response):
        self.response = response
        self.sent = []

    def request(self, method, params, **kwargs):
        self.sent.append((method, params))
        return self.response


def test_cancelled_turn_is_not_treated_as_an_answer():
    session = _bare_session()
    session._rpc = _FakeRpc({"stopReason": "cancelled"})

    with pytest.raises(RuntimeError, match="cancelled"):
        session.run("hi", 10, threading.Event())


def test_empty_turn_is_not_treated_as_an_answer():
    session = _bare_session()
    session._rpc = _FakeRpc({"stopReason": "end_turn"})

    with pytest.raises(RuntimeError, match="no final answer"):
        session.run("hi", 10, threading.Event())
