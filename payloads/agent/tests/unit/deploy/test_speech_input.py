"""``spec.ui.speech_input``: validation, the App env it projects, and its endpoint grant."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent_foundry.deploy.app_host import build_app_resources
from payloads.agent.config import resolve_agent_spec
from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.config import env_overrides_from_spec, speech_app_resources

ENDPOINT = "demo-speech"


def _spec(**speech: Any) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"ui": {"speech_input": speech}})


class TestContract:
    def test_off_by_default(self) -> None:
        speech = AgentPayloadSpec().ui.speech_input
        assert (speech.enabled, speech.backend, speech.max_seconds, speech.language) == (
            False,
            "serving_invocations",
            300,
            None,
        )

    @pytest.mark.parametrize(
        "speech",
        [
            {"enabled": True},
            {"enabled": True, "endpoint": "  "},
            {"enabled": True, "endpoint": ENDPOINT, "max_seconds": 301},
            {"enabled": True, "endpoint": ENDPOINT, "max_seconds": 0},
            {"enabled": True, "endpoint": ENDPOINT, "language": "deu"},
            {"enabled": True, "endpoint": ENDPOINT, "backend": "fmapi_chat"},
            {"enabled": True, "endpoint": ENDPOINT, "auto_send": True},
        ],
    )
    def test_invalid_settings_are_rejected(self, speech: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            _spec(**speech)


class TestEnv:
    def test_defaults_keep_the_mic_off(self) -> None:
        env = env_overrides_from_spec(AgentPayloadSpec())
        assert env["ENABLE_SPEECH_INPUT"] == "false"
        assert env["SPEECH_ENDPOINT"] == ""

    def test_enabled_projects_every_setting(self) -> None:
        env = env_overrides_from_spec(_spec(enabled=True, endpoint=ENDPOINT, max_seconds=120, language="de"))
        assert {key: env[key] for key in env if "SPEECH" in key} == {
            "ENABLE_SPEECH_INPUT": "true",
            "SPEECH_BACKEND": "serving_invocations",
            "SPEECH_ENDPOINT": ENDPOINT,
            "SPEECH_MAX_SECONDS": "120",
            "SPEECH_LANGUAGE": "de",
        }


class TestAppResource:
    def test_nothing_is_granted_while_off(self) -> None:
        assert speech_app_resources(_spec(endpoint=ENDPOINT)) == []

    def test_enabled_grants_can_query_on_the_endpoint(self) -> None:
        built = build_app_resources(speech_app_resources(_spec(enabled=True, endpoint=ENDPOINT)))
        assert [(r["name"], r["serving_endpoint"]["name"], r["serving_endpoint"]["permission"]) for r in built] == [
            ("speech-endpoint", ENDPOINT, "CAN_QUERY")
        ]


class TestBrickOutputPlaceholder:
    """``endpoint: ${var.serving_endpoint_name__speech}``, as the canary fixture writes it."""

    RAW = {"ui": {"speech_input": {"enabled": True, "endpoint": "${var.serving_endpoint_name__speech}"}}}

    def test_resolves_into_env_and_grant(self) -> None:
        spec = resolve_agent_spec(None, self.RAW, variables={"serving_endpoint_name__speech": ENDPOINT})
        assert env_overrides_from_spec(spec)["SPEECH_ENDPOINT"] == ENDPOINT
        built = build_app_resources(speech_app_resources(spec))
        assert [r["serving_endpoint"]["name"] for r in built] == [ENDPOINT]

    def test_unknown_variable_stays_as_written(self) -> None:
        spec = resolve_agent_spec(None, self.RAW, variables={})
        assert spec.ui.speech_input.endpoint == "${var.serving_endpoint_name__speech}"

    @pytest.mark.parametrize("project", [env_overrides_from_spec, speech_app_resources])
    def test_app_deploy_refuses_an_unresolved_endpoint(self, project: Any) -> None:
        spec = resolve_agent_spec(None, self.RAW, variables={})
        with pytest.raises(ValueError, match="serving_endpoint_name__speech"):
            project(spec)
