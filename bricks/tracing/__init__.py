"""The ``tracing`` deploy brick (issue #338; split from the runtime per #151).

Provisions the UC-backed MLflow experiment used for tracing.  Imported
by the orchestrator at deploy time only -- request-path code lives in
:mod:`payloads.agent.runtime`.
"""
