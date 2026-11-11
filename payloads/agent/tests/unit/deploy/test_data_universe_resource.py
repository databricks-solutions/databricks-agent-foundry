"""The data universe function grant, derived at bootstrap.

The app's service principal calls the overview function as itself on the
request path, so it needs ``EXECUTE`` on it before the first request.  The
grant is derived from the payload spec rather than authored in an
instance's ``app/config.yml``, because that file replaces the framework's
resource list wholesale -- authoring one entry there would silently drop
the tracing table grants with it.
"""

from __future__ import annotations

from typing import Any

from agent_foundry.deploy.app_host import build_app_resources
from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.config import data_universe_app_resources

VARIABLES = {"agent_schema": "cat.inst_agent"}


def _spec(data_universe: dict[str, Any]) -> AgentPayloadSpec:
    return AgentPayloadSpec.model_validate({"runtime": {"context": {"data_universe": data_universe}}})


class TestDataUniverseAppResources:
    def test_nothing_is_granted_while_the_block_is_off(self) -> None:
        assert data_universe_app_resources(_spec({"enabled": False}), VARIABLES) == []

    def test_default_spec_grants_nothing(self) -> None:
        assert data_universe_app_resources(AgentPayloadSpec(), VARIABLES) == []

    def test_enabling_the_block_grants_execute_on_the_function(self) -> None:
        resources = data_universe_app_resources(_spec({"enabled": True}), VARIABLES)

        assert resources == [
            {
                "name": "data-universe-function",
                "uc_securable": {
                    "securable_full_name": "cat.inst_agent.get_data_landscape",
                    "securable_type": "FUNCTION",
                    "permission": "EXECUTE",
                },
            }
        ]

    def test_the_grant_follows_the_authored_function_name(self) -> None:
        """One name, in ``payload.yml``.  A second copy is a second thing to drift."""
        resources = data_universe_app_resources(
            _spec({"enabled": True, "function": "other_cat.other_sch.landscape"}),
            VARIABLES,
        )

        assert resources[0]["uc_securable"]["securable_full_name"] == "other_cat.other_sch.landscape"

    def test_an_unqualifiable_name_is_skipped_rather_than_fatal(self) -> None:
        """A missing ``agent_schema`` already disables injection at runtime;
        the deploy should warn and carry on, not fail the app spec."""
        assert data_universe_app_resources(_spec({"enabled": True}), {}) == []

    def test_the_entry_survives_the_rest_payload_builder(self) -> None:
        resources = data_universe_app_resources(_spec({"enabled": True}), VARIABLES)

        built = build_app_resources(resources)

        assert [r["name"] for r in built] == ["data-universe-function"]
        assert built[0]["uc_securable"]["permission"] == "EXECUTE"
