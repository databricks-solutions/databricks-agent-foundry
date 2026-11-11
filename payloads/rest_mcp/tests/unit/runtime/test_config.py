"""Tests for the ``rest_mcp`` runtime config loader.

A valid ``payload.yml`` spec round-trips to a typed ``RestMcpPayloadSpec``; an
absent/empty spec fails closed (a proxy must declare at least one API).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

_VALID_PAYLOAD_YML = """
payload:
  adapter: rest_mcp
  spec:
    apis:
      - name: orders
        rest:
          base_url: https://api.example.com
          allowed_hosts: [api.example.com]
          auth:
            mode: none
        tools:
          - name: get_status
            description: Get order status.
            request:
              method: GET
              path: /status
"""


def _write_payload(instance_dir: Path, body: str) -> None:
    payload_dir = instance_dir / "payloads" / "rest_mcp"
    payload_dir.mkdir(parents=True, exist_ok=True)
    (payload_dir / "payload.yml").write_text(body, encoding="utf-8")


def test_loads_valid_spec(tmp_path: Path) -> None:
    _write_payload(tmp_path, _VALID_PAYLOAD_YML)
    spec = load_rest_mcp_spec(tmp_path)
    assert [api.name for api in spec.apis] == ["orders"]
    assert spec.apis[0].tools[0].name == "get_status"


def test_absent_spec_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_rest_mcp_spec(tmp_path)


def test_empty_spec_fails_closed(tmp_path: Path) -> None:
    _write_payload(tmp_path, "payload:\n  adapter: rest_mcp\n  spec:\n    apis: []\n")
    with pytest.raises(ValidationError):
        load_rest_mcp_spec(tmp_path)
