"""Tests for the per-chat plan's updates, rendering and recitation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from payloads.agent.runtime.plan import (
    FINISHED_HEADING,
    MAX_FINISHED_SHOWN,
    MAX_MILESTONES,
    NUDGE_AFTER_CALLS,
    PLAN_HEADING,
    Plan,
    PlanState,
    PlanUpdate,
    PlanUpdateError,
    apply_update,
    archive_plan,
    parse_plan,
    plan_goal,
    plan_is_open,
    render_finished_goals,
    render_plan,
    render_plan_block,
    replaces_finished,
)

_OPEN = "**Goal:** Q3\n1. [x] Inventory — 412 tables\n2. [ ] Costs"
_FINISHED = "**Goal:** Q2\n1. [x] Inventory — ok\n2. [-] Lineage — no access"


def _update(**args: Any) -> PlanUpdate:
    return PlanUpdate.model_validate(args)


def _plan(*milestones: str, goal: str = "Q3") -> Plan:
    return apply_update(None, _update(goal=goal, add=list(milestones)))


class TestPlanUpdate:
    @pytest.mark.parametrize(
        "args",
        [
            {},
            {"goal": ""},
            {"add": ["  "]},
            {"add": ["a"] * (MAX_MILESTONES + 1)},
            {"done": [{"milestone": 0, "result": "r"}]},
            {"done": [{"milestone": 1, "result": ""}]},
            {"drop": [{"milestone": 1}]},
            {"goal": "g", "steps": []},
        ],
    )
    def test_invalid_updates_are_rejected(self, args: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            PlanUpdate.model_validate(args)


class TestApplyUpdate:
    def test_starts_a_plan_from_a_goal_and_milestones(self) -> None:
        plan = _plan("Inventory", "Costs", goal="Find\nthe  drop")
        assert plan.goal == "Find the drop"
        assert [(m.title, m.status) for m in plan.milestones] == [("Inventory", "open"), ("Costs", "open")]

    def test_marks_several_milestones_and_adds_after_the_shown_numbers(self) -> None:
        update = _update(
            done=[{"milestone": 1, "result": "412\ntables"}],
            drop=[{"milestone": 2, "reason": "no access"}],
            add=["Report"],
        )
        plan = apply_update(_plan("Inventory", "Lineage"), update)
        assert [(m.title, m.status, m.note) for m in plan.milestones] == [
            ("Inventory", "done", "412 tables"),
            ("Lineage", "dropped", "no access"),
            ("Report", "open", ""),
        ]

    def test_a_goal_on_an_open_plan_changes_only_the_goal(self) -> None:
        plan = apply_update(_plan("Inventory"), _update(goal="Q4"))
        assert plan.goal == "Q4" and [m.title for m in plan.milestones] == ["Inventory"]

    def test_a_new_goal_after_a_finished_plan_starts_over(self) -> None:
        finished = apply_update(_plan("Inventory"), _update(done=[{"milestone": 1, "result": "ok"}]))
        update = _update(goal="Q4", add=["Costs"])
        assert replaces_finished(finished, update)
        assert [m.title for m in apply_update(finished, update).milestones] == ["Costs"]

    def test_adding_to_a_finished_plan_reopens_it(self) -> None:
        finished = apply_update(_plan("Inventory"), _update(done=[{"milestone": 1, "result": "ok"}]))
        assert not replaces_finished(finished, _update(goal="Q3", add=["Costs"]))
        assert apply_update(finished, _update(add=["Costs"])).is_open

    @pytest.mark.parametrize(
        ("current", "args", "message"),
        [
            (None, {"add": ["a"]}, "there is no plan yet"),
            (None, {"goal": "g"}, "at least one milestone"),
            ("one", {"done": [{"milestone": 2, "result": "r"}]}, "there is no milestone 2"),
            ("one", {"add": ["a"] * MAX_MILESTONES}, f"at most {MAX_MILESTONES} milestones"),
        ],
    )
    def test_updates_that_do_not_fit_say_why(self, current: str | None, args: dict[str, Any], message: str) -> None:
        with pytest.raises(PlanUpdateError, match=message):
            apply_update(_plan(current) if current else None, _update(**args))

    def test_does_not_change_the_current_plan(self) -> None:
        current = _plan("Inventory")
        apply_update(current, _update(done=[{"milestone": 1, "result": "ok"}]))
        assert current.milestones[0].status == "open"


class TestRendering:
    def test_renders_numbered_milestones_with_their_notes(self) -> None:
        plan = apply_update(_plan("Inventory", "Costs"), _update(done=[{"milestone": 1, "result": "412 tables"}]))
        assert render_plan(plan) == _OPEN

    def test_round_trips_through_the_stored_text(self) -> None:
        plan = apply_update(_plan("A — B", "C"), _update(drop=[{"milestone": 2, "reason": "x — y"}]))
        assert parse_plan(render_plan(plan)) == plan
        assert plan.milestones[0].title == "A - B"

    def test_reads_plans_saved_as_step_checklists(self) -> None:
        legacy = parse_plan("**Goal:** Q3\n- [x] Step 1\n- [>] Step 2\n- [ ] Step 3")
        assert [m.status for m in legacy.milestones] == ["done", "open", "open"]
        assert not plan_is_open("**Goal:** Q3\n- [x] Step 1")

    def test_open_plan_is_shown_and_a_finished_one_is_not(self) -> None:
        assert plan_is_open(_OPEN)
        assert render_plan_block(_OPEN).startswith(f"{PLAN_HEADING}\n{_OPEN}\n")
        assert render_plan_block("**Goal:** Q3\n1. [x] A — ok\n2. [-] B — no access") == ""

    def test_reminds_after_calls_without_an_update(self) -> None:
        assert "Not updated" not in render_plan_block(_OPEN, NUDGE_AFTER_CALLS - 1)
        assert f"Not updated for {NUDGE_AFTER_CALLS} model calls" in render_plan_block(_OPEN, NUDGE_AFTER_CALLS)


class TestPlanState:
    def test_recites_the_latest_saved_plan(self) -> None:
        state = PlanState()
        assert state.recite() == ""
        state.saved(_OPEN)
        assert state.recite().startswith(f"{PLAN_HEADING}\n{_OPEN}")
        state.saved("")
        assert state.recite() == ""

    def test_counts_calls_and_resets_on_save(self) -> None:
        state = PlanState(_OPEN)
        blocks = [state.recite() for _ in range(NUDGE_AFTER_CALLS + 1)]
        assert "Not updated" not in blocks[-2] and "Not updated" in blocks[-1]
        state.saved(_OPEN)
        assert "Not updated" not in state.recite()

    def test_recites_the_finished_goals_after_the_open_plan(self) -> None:
        state = PlanState(_OPEN, _FINISHED)
        plan_block, finished = state.recite().split("\n\n")
        assert plan_block.startswith(PLAN_HEADING) and finished == f"{FINISHED_HEADING}\n- Q2 (1 done, 1 dropped)"

    def test_an_archive_updates_the_finished_goals(self) -> None:
        state = PlanState(_OPEN)
        state.archived(_FINISHED)
        assert "- Q2 (1 done, 1 dropped)" in state.recite()


class TestFinishedGoals:
    def test_empty_without_finished_plans(self) -> None:
        assert render_finished_goals("", _OPEN) == ""

    def test_lists_the_history_then_the_current_plan_once_finished(self) -> None:
        current = "**Goal:** Q3\n1. [x] Inventory — ok"
        assert render_finished_goals(_FINISHED, current).splitlines()[1:] == [
            "- Q2 (1 done, 1 dropped)",
            "- Q3 (1 done)",
        ]

    def test_shows_only_the_latest(self) -> None:
        history = "\n\n".join(f"**Goal:** G{n}\n1. [x] a — ok" for n in range(MAX_FINISHED_SHOWN + 2))
        lines = render_finished_goals(history, "").splitlines()[1:]
        assert len(lines) == MAX_FINISHED_SHOWN and lines[-1] == f"- G{MAX_FINISHED_SHOWN + 1} (1 done)"


class TestHistory:
    def test_goal_is_read_back_from_the_stored_plan(self) -> None:
        assert plan_goal(render_plan(_plan("a", goal="Find\nthe drop"))) == "Find the drop"
        assert plan_goal("") == ""

    def test_archive_appends_plans(self) -> None:
        first, second = render_plan(_plan("a", goal="A")), render_plan(_plan("a", goal="B"))
        history = archive_plan(archive_plan("", first, 1_000), second, 1_000)
        assert history == f"{first}\n\n{second}"

    def test_archive_drops_the_oldest_plans_beyond_the_limit(self) -> None:
        plans = [render_plan(_plan("a", goal=goal)) for goal in "ABC"]
        history = archive_plan(archive_plan(plans[0], plans[1], 1_000), plans[2], len(plans[1]) + 2 + len(plans[2]))
        assert history == f"{plans[1]}\n\n{plans[2]}"

    def test_archive_keeps_the_newest_plan_even_alone_over_the_limit(self) -> None:
        plan = render_plan(_plan("a"))
        assert archive_plan("old", plan, 5) == plan
