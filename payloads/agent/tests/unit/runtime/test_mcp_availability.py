"""Notices and trace labels for unavailable MCP servers."""

from __future__ import annotations

from payloads.agent.runtime.mcp_availability import UnavailableServer, model_note, trace_label, user_notice

JIRA = UnavailableServer("jira", "connect_failed")
GITHUB = UnavailableServer("main.tools.github", "no_user_token")


def test_nothing_unavailable_renders_nothing() -> None:
    assert model_note(()) == user_notice(()) == trace_label(()) == ""


def test_model_note_names_servers_and_forbids_substitutes() -> None:
    note = model_note((JIRA, GITHUB))

    assert note.startswith("## Unavailable tools\n")
    assert "jira, main.tools.github" in note
    assert "Do not substitute" in note


def test_user_notice_lists_each_name_once_sorted() -> None:
    notice = user_notice((JIRA, GITHUB, UnavailableServer("jira", "invalid_state")))

    assert notice == (
        "Some tools are unavailable right now: jira, main.tools.github. Answers that need them may be incomplete."
    )


def test_trace_label_keeps_reasons() -> None:
    assert trace_label((JIRA, GITHUB)) == "jira|connect_failed,main.tools.github|no_user_token"
