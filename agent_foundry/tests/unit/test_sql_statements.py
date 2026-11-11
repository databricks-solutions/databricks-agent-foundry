"""Tests for ``agent_foundry.sql_statements``."""

from __future__ import annotations

from typing import Any

import pytest

from agent_foundry.sql_statements import ensure_schema, quote_dotted_name, quote_identifier, quote_string, tag_schema


def _recording_client(calls: list[str]) -> Any:
    class FakeClient:
        class api_client:
            @staticmethod
            def do(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
                calls.append(kwargs.get("body", {}).get("statement", ""))
                return {"status": {"state": "SUCCEEDED"}}

    return FakeClient()


class TestTagSchema:
    """Test tag_schema SQL generation."""

    def test_generates_correct_alter_statement(self) -> None:
        calls: list[str] = []
        tag_schema(_recording_client(calls), "wh1", "my_catalog", "my_schema", {"ai_solution_managed_by": "test-app"})
        assert len(calls) == 1
        assert "ALTER SCHEMA `my_catalog`.`my_schema` SET TAGS" in calls[0]
        assert "'ai_solution_managed_by' = 'test-app'" in calls[0]

    def test_multiple_tags(self) -> None:
        calls: list[str] = []
        tag_schema(_recording_client(calls), "wh1", "c", "s", {"ai_solution_managed_by": "my-app", "env": "dev"})
        assert "'ai_solution_managed_by' = 'my-app'" in calls[0]
        assert "'env' = 'dev'" in calls[0]

    def test_empty_tags_is_noop(self) -> None:
        tag_schema(None, "wh1", "c", "s", {})

    def test_escapes_tag_keys_values_and_names(self) -> None:
        calls: list[str] = []
        tag_schema(_recording_client(calls), "wh1", "c`at", "s", {"o'k": "v\\"})
        assert calls == ["ALTER SCHEMA `c``at`.`s` SET TAGS ('o\\'k' = 'v\\\\')"]


class TestEnsureSchema:
    def test_escapes_backticks_in_names(self) -> None:
        calls: list[str] = []
        ensure_schema(_recording_client(calls), "wh1", "cat", "sch`ema")
        assert calls == ["CREATE SCHEMA IF NOT EXISTS `cat`.`sch``ema`"]


class TestQuoteIdentifier:
    def test_quotes_each_part(self) -> None:
        assert quote_identifier("cat", "sch", "t") == "`cat`.`sch`.`t`"

    def test_doubles_embedded_backticks(self) -> None:
        assert quote_identifier("a`b") == "`a``b`"


class TestQuoteDottedName:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("my-cat", "`my-cat`"),
            ("my-cat.sch.t", "`my-cat`.`sch`.`t`"),
            ("`my-cat`.sch", "`my-cat`.`sch`"),
            ("`a.b`.c", "`a.b`.`c`"),
            ("`a``b`.c", "`a``b`.`c`"),
        ],
    )
    def test_quotes_each_part(self, name: str, expected: str) -> None:
        assert quote_dotted_name(name) == expected


class TestQuoteString:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("plain", "'plain'"),
            ("it's", "'it\\'s'"),
            ("trailing\\", "'trailing\\\\'"),
            ("x\\' OR 1=1 --", "'x\\\\\\' OR 1=1 --'"),
        ],
        ids=["plain", "quote", "trailing-backslash", "backslash-quote"],
    )
    def test_escapes_backslash_before_quote(self, value: str, expected: str) -> None:
        assert quote_string(value) == expected
