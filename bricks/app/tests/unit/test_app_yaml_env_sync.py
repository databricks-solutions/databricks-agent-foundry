"""Sync test: bricks/app/app.yaml vs the env-var registry (#158).

The registry in :mod:`agent_foundry.env` is the single source of truth
for which environment variables the deployed Databricks App needs at
its boundary.  ``vars_for("app")`` enumerates those.

This test asserts that every such variable is declared in the framework
default ``bricks/app/app.yaml`` -- adding a new required ``app``
context variable to the registry without updating the template fails
CI here, closing the drift gap that #158 was opened to fix.

Note on scope
-------------
The original #158 framing assumed two ``app.yaml`` templates would need
generation (``bricks/app/`` and ``bricks/app/``).  After #170
deleted the orphan ``bricks/app/`` brick (per #152), only the
``agent_app`` template remains.  A single sync test is therefore the
minimal mechanism that satisfies the acceptance criterion "no drift
possible -- adding a new required env var fails CI until both templates
are regenerated".  Instances may still override individual entries by
shipping their own ``app/app.yaml`` (the framework template is
merged by name; see the header comment in the YAML); this test only
guards the framework default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_foundry.env import EnvVar, vars_for

APP_YAML = Path(__file__).resolve().parents[2] / "app.yaml"


def _declared_env_names() -> set[str]:
    """Return the set of ``env[*].name`` entries declared in app.yaml."""
    raw = yaml.safe_load(APP_YAML.read_text(encoding="utf-8")) or {}
    entries = raw.get("env", []) or []
    return {entry["name"] for entry in entries if "name" in entry}


def _capability_derived_names() -> set[str]:
    """Env vars sourced from the payload spec, not app.yaml (#338).

    These runtime toggles are the payload's concern: the agent payload
    adapter derives them from the ``payloads/agent/payload.yml`` ``spec``
    and injects them over the infra-only brick default.  Sourced from the
    deriver itself so this stays in sync automatically.
    """
    from payloads.agent.contracts import AgentPayloadSpec
    from payloads.agent.deploy.config import env_overrides_from_spec

    return set(env_overrides_from_spec(AgentPayloadSpec()))


class TestAppYamlEnvRegistrySync:
    def test_every_app_required_var_is_declared(self) -> None:
        # Tripwire: anything ``required_at=("app", ...)`` in
        # agent_foundry/env.py REGISTRY must be supplied to the deployed
        # App -- either authored in the infra-only app.yaml or derived
        # from payload capabilities by the adapter (issue #338).
        declared = _declared_env_names()
        capability_derived = _capability_derived_names()
        required = {v.name for v in vars_for("app")}
        missing = sorted(required - declared - capability_derived)
        assert not missing, (
            f"bricks/app/app.yaml is missing entries for env vars "
            f"required at the 'app' context: {missing}. Either add them "
            f"to app.yaml, derive them from payload capabilities, or "
            f"remove the 'app' tag from agent_foundry/env.py REGISTRY."
        )

    def test_capability_vars_not_authored_in_app_yaml(self) -> None:
        # Reverse guard: capability-derived toggles must NOT be authored
        # in the infra-only brick default -- they are the payload's single
        # source (payloads/agent/payload.yml) and are injected by the
        # adapter.  Double-authoring would reintroduce the drift #338 removed.
        double_authored = sorted(_declared_env_names() & _capability_derived_names())
        assert not double_authored, (
            f"bricks/app/app.yaml authors capability-derived toggles "
            f"{double_authored}; remove them -- they are derived from "
            f"payload capabilities by the agent payload adapter."
        )

    def test_no_unknown_app_yaml_entries(self) -> None:
        # Reverse tripwire: every name in app.yaml should correspond to
        # a registered EnvVar.  Catches the opposite drift -- the
        # template grows a key that the registry doesn't know about.
        # ``valueFrom``-style entries (e.g. MLFLOW_EXPERIMENT_ID bound
        # via App resources, not required at the 'app' boundary itself)
        # still need a registry entry; their context just doesn't
        # include "app".
        declared = _declared_env_names()
        from agent_foundry.env import REGISTRY

        known = {v.name for v in REGISTRY}
        unknown = sorted(declared - known)
        assert not unknown, (
            f"bricks/app/app.yaml declares env vars not in the "
            f"registry: {unknown}. Add them to agent_foundry/env.py "
            f"REGISTRY or remove from app.yaml."
        )

    @pytest.mark.parametrize("var", vars_for("app"))
    def test_each_app_var_has_a_value_or_value_from(self, var: EnvVar) -> None:
        # Each entry must declare either a literal ``value`` or a
        # ``valueFrom`` resource binding.  ``value: ""`` is the
        # platform's "unset" marker, which would silently boot the App
        # with an empty string for what the registry says is required.
        # Capability-derived toggles are injected by the adapter, not
        # authored here (issue #338), so they are out of scope.
        if var.name in _capability_derived_names():
            pytest.skip("capability-derived; injected by the agent payload adapter, not app.yaml")
        raw = yaml.safe_load(APP_YAML.read_text(encoding="utf-8")) or {}
        entries = raw.get("env", []) or []
        match = next((e for e in entries if e.get("name") == var.name), None)
        assert match is not None, f"missing entry for {var.name}"
        has_value = "value" in match and match["value"] not in (None, "")
        has_value_from = "valueFrom" in match and match["valueFrom"]
        assert has_value or has_value_from, (
            f"app.yaml entry for {var.name!r} has neither a non-empty `value` nor a `valueFrom` binding."
        )
