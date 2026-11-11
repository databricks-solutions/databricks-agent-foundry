"""Deploy-time glue for the built-in ``agent`` App payload (issue #338).

This package owns the agent payload's *deploy-time* code -- the
provisioning, config and runtime/infra spec resolution that the generic
App host (:mod:`agent_foundry.deploy.app_host`) drives through the
:class:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter` seam.

It is deploy-time only (NOT a runtime root): unlike
``payloads/agent/runtime``, modules here may import the framework
``agent_foundry.deploy`` layer.
"""
