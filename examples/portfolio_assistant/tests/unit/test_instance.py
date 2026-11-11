"""Offline validation tests for the portfolio-assistant instance.

Verifies that all config files are well-formed and consistent without
requiring a live workspace. Run with:

    python -m pytest instances/portfolio-assistant/tests/unit -v

Framework files (``bricks/app/app.yaml``, dashboard templates,
``validate_index`` helpers) are loaded via :mod:`importlib.resources`
from the installed ``agent-foundry`` wheel rather than local-tree
paths -- the sample-agent only ships ``instances/`` and ``conf/``;
the framework lives in ``site-packages/`` after ``uv sync``.
"""
from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# foundry.toml (per-instance, replaces the pre-#191 instance.yml contract)
# ---------------------------------------------------------------------------

class TestInstanceFoundryToml:

    def test_has_required_fields(self, instance_config: dict):
        assert "name" in instance_config
        assert "description" in instance_config

    def test_name_matches_directory(self, instance_dir: Path, instance_config: dict):
        assert instance_config["name"] == instance_dir.name

    def test_identity_fields_defined(self, instance_config: dict):
        # ``FoundryInstanceConfig`` flattens the pre-#191 ``variables`` block
        # into top-level fields -- the audience boundary is structural, not
        # a free-form bag.  ``app_name`` / ``chat_ui_app_name`` are not on
        # this list any more: they derive from ``name`` when omitted (see
        # FoundryInstanceConfig._derive_app_names_from_name).  This test
        # checks only the fields the author still has to provide.
        assert "vs_endpoint_name" in instance_config, "vs_endpoint_name required"
        assert "schema_prefix" in instance_config, "schema_prefix required"

    def test_app_name_resolves_to_instance_name(self, instance_config: dict):
        # When omitted from the on-disk TOML, ``app_name`` derives from
        # ``name`` via the FoundryInstanceConfig model_validator.  This
        # test verifies the *resolved* shape rather than the raw key
        # (the raw key is intentionally omitted for clean authoring).
        from agent_foundry import FoundryInstanceConfig

        cfg = FoundryInstanceConfig.model_validate(instance_config)
        assert cfg.app_name == cfg.name
        assert cfg.chat_ui_app_name == f"{cfg.name}-ui"


# ---------------------------------------------------------------------------
# payloads/agent/payload.yml (single agent config contract)
# ---------------------------------------------------------------------------

class TestAgentConfig:

    def test_config_file_exists(self, instance_dir: Path):
        assert (instance_dir / "payloads" / "agent" / "payload.yml").is_file()

    def test_loads_with_brick_loader(self, instance_dir: Path):
        from payloads.agent.runtime.config import load_agent_config

        config = load_agent_config(instance_dir)
        assert config["LLM_ENDPOINT_NAME"], "LLM endpoint must be set"
        assert config["SYSTEM_PROMPT"], "System prompt must be set"
        assert isinstance(config["MAX_ITERATIONS"], int)
        assert config["MAX_ITERATIONS"] > 0

    def test_system_prompt_is_meaningful(self, instance_dir: Path):
        from payloads.agent.runtime.config import load_agent_config

        config = load_agent_config(instance_dir)
        assert len(config["SYSTEM_PROMPT"]) > 20, "System prompt too short"


# ---------------------------------------------------------------------------
# app (brick default + optional instance overrides)
# ---------------------------------------------------------------------------

BRICK_APP_YAML = files("bricks.app").joinpath("app.yaml")
BRICK_APP_DEFAULTS = files("bricks.app").joinpath("_scaffold", "config.yml")


class TestAgentAppYaml:

    def test_brick_default_exists(self):
        assert BRICK_APP_YAML.is_file(), "Brick default app.yaml must exist"

    def test_has_command(self):
        data = yaml.safe_load(BRICK_APP_YAML.read_text()) or {}
        assert "command" in data, "app.yaml must define a command"

    def test_agent_module_env_set(self):
        data = yaml.safe_load(BRICK_APP_YAML.read_text()) or {}
        env_vars = {e["name"]: e.get("value", "") for e in data.get("env", [])}
        assert "AGENT_MODULE" in env_vars, "AGENT_MODULE env var required"
        assert env_vars["AGENT_MODULE"] == "payloads.agent.runtime.agent"

    def test_brick_defaults_config_exists(self):
        assert BRICK_APP_DEFAULTS.is_file(), "Brick default config.yml must exist"

    def test_instance_config_is_optional_overrides(self, instance_dir: Path):
        cfg_path = instance_dir / "app" / "config.yml"
        if not cfg_path.exists():
            return
        with open(cfg_path) as f:
            data = yaml.safe_load(f) or {}
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# data_domains/
# ---------------------------------------------------------------------------

class TestDataDomains:

    def test_at_least_one_domain(self, instance_dir: Path):
        dd_dir = instance_dir / "data_domains"
        if not dd_dir.exists():
            return
        domains = [d for d in dd_dir.iterdir() if d.is_dir() and (d / "domain.yml").exists()]
        assert len(domains) >= 1

    def test_domain_yml_valid(self, instance_dir: Path):
        dd_dir = instance_dir / "data_domains"
        if not dd_dir.exists():
            return
        for domain_dir in dd_dir.iterdir():
            if not domain_dir.is_dir():
                continue
            domain_yml = domain_dir / "domain.yml"
            if not domain_yml.exists():
                continue
            with open(domain_yml) as f:
                data = yaml.safe_load(f) or {}
            assert "domain" in data, f"{domain_yml}: missing 'domain' key"

    def test_docs_corpus_declares_volume_and_doc_uri(self, instance_dir: Path):
        docs_domain = instance_dir / "data_domains" / "docs_corpus"
        volume_sql = docs_domain / "volumes" / "00_docs_sources.sql"
        table_sql = docs_domain / "tables" / "01_docs.sql"

        assert volume_sql.exists()
        assert "CREATE VOLUME IF NOT EXISTS" in volume_sql.read_text()
        assert "doc_uri STRING" in table_sql.read_text()


# ---------------------------------------------------------------------------
# genie_spaces/
# ---------------------------------------------------------------------------

class TestGenieSpaces:

    def test_at_least_one_space(self, instance_dir: Path):
        gs_dir = instance_dir / "genie_spaces"
        assert gs_dir.exists(), "genie_spaces/ directory is required"
        spaces = [d for d in gs_dir.iterdir() if d.is_dir() and (d / "space.yml").exists()]
        assert len(spaces) >= 1

    def test_space_yml_valid(self, instance_dir: Path):
        # Per the framework contract (issue #84+), ``title``,
        # ``warehouse_id``, and ``parent_path`` are computed from
        # ``InstanceConfig`` and injected by the assembler -- they must
        # NOT appear in the user-authored ``space.yml``.  The author
        # contract is just ``description`` (human-readable).
        gs_dir = instance_dir / "genie_spaces"
        for space_dir in gs_dir.iterdir():
            if not space_dir.is_dir():
                continue
            space_yml = space_dir / "space.yml"
            if not space_yml.exists():
                continue
            with open(space_yml) as f:
                data = yaml.safe_load(f) or {}
            assert "description" in data, f"{space_yml}: missing 'description'"
            # Auto-computed fields must not be hand-set.
            for forbidden in ("title", "warehouse_id", "parent_path"):
                assert forbidden not in data, (
                    f"{space_yml}: '{forbidden}' is auto-computed by the assembler "
                    "and must not be set in space.yml"
                )

    def test_has_data_sources(self, instance_dir: Path):
        gs_dir = instance_dir / "genie_spaces"
        for space_dir in gs_dir.iterdir():
            if not space_dir.is_dir():
                continue
            tables_yml = space_dir / "data_sources" / "tables.yml"
            assert tables_yml.exists(), f"{space_dir.name}: missing data_sources/tables.yml"
            with open(tables_yml) as f:
                data = yaml.safe_load(f) or {}
            assert "tables" in data, f"{tables_yml}: missing 'tables' key"
            assert len(data["tables"]) >= 1, f"{tables_yml}: need at least one table"

    def test_has_instructions(self, instance_dir: Path):
        gs_dir = instance_dir / "genie_spaces"
        for space_dir in gs_dir.iterdir():
            if not space_dir.is_dir():
                continue
            instr_dir = space_dir / "instructions"
            assert instr_dir.exists(), f"{space_dir.name}: missing instructions/"
            assert (instr_dir / "text_instructions.md").exists(), (
                f"{space_dir.name}: missing text_instructions.md"
            )

    def test_has_sample_questions(self, instance_dir: Path):
        gs_dir = instance_dir / "genie_spaces"
        for space_dir in gs_dir.iterdir():
            if not space_dir.is_dir():
                continue
            sq = space_dir / "config" / "sample_questions.yml"
            if not sq.exists():
                continue
            with open(sq) as f:
                data = yaml.safe_load(f) or {}
            questions = data.get("questions", [])
            assert len(questions) >= 1, f"{sq}: need at least one sample question"


# ---------------------------------------------------------------------------
# evaluation/
# ---------------------------------------------------------------------------

class TestEvaluation:

    def test_eval_config_exists(self, instance_dir: Path):
        assert (instance_dir / "evaluation" / "eval_config.yml").exists()

    def test_eval_config_valid(self, instance_dir: Path):
        with open(instance_dir / "evaluation" / "eval_config.yml") as f:
            data = yaml.safe_load(f) or {}
        assert "scorers" in data
        assert "default" in data["scorers"]
        assert len(data["scorers"]["default"]) >= 1

    def test_golden_dataset_exists(self, instance_dir: Path):
        ds_dir = instance_dir / "evaluation" / "datasets"
        assert ds_dir.exists(), "evaluation/datasets/ directory required"
        yamls = list(ds_dir.glob("*.yml"))
        assert len(yamls) >= 1, "At least one golden dataset YAML required"

    def test_golden_dataset_valid(self, instance_dir: Path):
        for ds_path in (instance_dir / "evaluation" / "datasets").glob("*.yml"):
            with open(ds_path) as f:
                data = yaml.safe_load(f) or {}
            entries = data.get("dataset", [])
            assert len(entries) >= 1, f"{ds_path.name}: need at least one entry"
            for entry in entries:
                assert "inputs" in entry, f"{ds_path.name}: entry missing 'inputs'"
                assert "question" in entry["inputs"], f"{ds_path.name}: input missing 'question'"

    def test_production_monitoring_configured(self, instance_dir: Path):
        with open(instance_dir / "evaluation" / "eval_config.yml") as f:
            data = yaml.safe_load(f) or {}
        pm = data.get("production_monitoring", {})
        assert "sample_rate" in pm
        assert "scorers" in pm
        assert len(pm["scorers"]) >= 1


# ---------------------------------------------------------------------------
# agent tracing & auth
# ---------------------------------------------------------------------------
#
# The previous ``TestTracingAndAuth`` class asserted ``TRACING_ENABLED``,
# ``TRACING_EXPERIMENT``, and ``TRACING_LOG_INPUTS`` keys in the loaded
# agent config.  The framework's tracing cleanup removed those keys (see
# ``payloads/agent/runtime/config.py`` and
# ``bricks/observability/docs/field-guide.md``); the previous switches
# were a dead opt-in stub.  Tracing is now always-on via the
# ``@invoke()`` / ``@stream()`` decorators and tracing destination is
# computed from ``InstanceConfig`` at deploy time.
#
# Redaction is now controlled by the runtime env vars
# ``MLFLOW_TRACE_REDACT_INPUTS`` / ``MLFLOW_TRACE_REDACT_OUTPUTS`` set in
# ``app/app.yaml`` overrides; there is nothing for an offline
# config-file test to assert.


# ---------------------------------------------------------------------------
# observability/
# ---------------------------------------------------------------------------

class TestObservability:
    """Observability config is fully derived from InstanceConfig.
    The dashboard template lives in the framework's observability brick,
    not in the instance."""

    def test_dashboard_template_exists(self):
        template = files("bricks.observability").joinpath(
            "dashboards", "template.lvdash.json"
        )
        assert template.is_file(), "Dashboard template must ship with the framework wheel"


# ---------------------------------------------------------------------------
# vector_indexes/
# ---------------------------------------------------------------------------

class TestVectorIndexes:

    def test_at_least_one_index(self, instance_dir: Path):
        vi_dir = instance_dir / "vector_indexes"
        if not vi_dir.exists():
            return
        indexes = [d for d in vi_dir.iterdir() if d.is_dir() and (d / "index_config.yml").exists()]
        assert len(indexes) >= 1

    def test_index_config_validates(self, instance_dir: Path):
        from bricks.vector_search.scripts.validate_index import (
            load_config,
            substitute_variables,
            validate_index_config,
        )

        vi_dir = instance_dir / "vector_indexes"
        if not vi_dir.exists():
            return

        for idx_dir in vi_dir.iterdir():
            cfg_path = idx_dir / "index_config.yml"
            if not cfg_path.exists():
                continue

            config = load_config(cfg_path)
            config = substitute_variables(config, {
                "catalog": "test_catalog",
                "schema": "test_schema",
                "schema_prefix": "test",
                "vs_endpoint_name": "test_endpoint",
            })
            validate_index_config(config, source=cfg_path)

    def test_docs_search_returns_doc_uri(self, instance_dir: Path):
        index_path = instance_dir / "vector_indexes" / "docs_search" / "index_config.yml"
        server_path = (
            instance_dir
            / "payloads"
            / "agent"
            / "mcp_servers"
            / "docs_search"
            / "server.yml"
        )

        index_config = yaml.safe_load(index_path.read_text())
        columns_to_sync = index_config["index"]["delta_sync"]["columns_to_sync"]
        server_config = yaml.safe_load(server_path.read_text())
        result_columns = server_config["tools"]["default_meta"]["columns"].split(",")

        assert "doc_uri" in columns_to_sync
        assert "doc_uri" in result_columns


# ---------------------------------------------------------------------------
# jobs/docs_ingest
# ---------------------------------------------------------------------------


class TestDocsIngest:
    """Jobs API rejects this trigger unless the URL ends with ``/``
    and ``wait_after_last_change_seconds`` is greater than 60 (#435, #436).
    """

    def test_file_arrival_trigger_meets_jobs_api_constraints(self, instance_dir: Path):
        from agent_foundry.contracts.jobs import JobConfig

        job_path = instance_dir / "jobs" / "docs_ingest" / "job.yml"
        config = JobConfig.model_validate(yaml.safe_load(job_path.read_text()))

        assert config.trigger is not None
        assert config.trigger.file_arrival is not None
        arrival = config.trigger.file_arrival
        assert arrival.url.endswith("/"), "Jobs API requires a trailing slash on file_arrival.url"
        assert arrival.wait_after_last_change_seconds is not None
        assert arrival.wait_after_last_change_seconds > 60

    def test_ingest_notebook_exists(self, instance_dir: Path):
        job_dir = instance_dir / "jobs" / "docs_ingest"
        config = yaml.safe_load((job_dir / "job.yml").read_text())
        notebook = config["tasks"][0]["notebook_task"]["notebook_path"]
        assert (job_dir / notebook.removeprefix("./")).is_file()


# ---------------------------------------------------------------------------
# jobs/ + sql_functions/ for the data universe
# ---------------------------------------------------------------------------

class TestDataUniverse:
    """Guards for the injected catalog overview.

    Nothing in framework CI exercises an example's jobs or SQL functions,
    so these cheap structural checks are the only thing standing between a
    typo here and a failed deploy.
    """

    def test_profile_job_validates(self, instance_dir: Path):
        from agent_foundry.contracts.jobs import JobConfig

        job_path = instance_dir / "jobs" / "data_universe_profile" / "job.yml"
        config = JobConfig.model_validate(yaml.safe_load(job_path.read_text()))

        assert config.schedule is not None, "the landscape has to be refreshed on some cadence"
        assert len(config.tasks) == 1

    def test_profile_job_runs_a_notebook_that_exists(self, instance_dir: Path):
        """``sql_task`` validates but never deploys: only notebook and
        spark_python sources are uploaded by the jobs brick."""
        job_dir = instance_dir / "jobs" / "data_universe_profile"
        config = yaml.safe_load((job_dir / "job.yml").read_text())
        task = config["tasks"][0]

        assert "notebook_task" in task, "a sql_task here would fail at deploy time"
        notebook = task["notebook_task"]["notebook_path"]
        assert (job_dir / notebook.removeprefix("./")).is_file()

    def test_landscape_tables_are_declared_before_the_functions(self, instance_dir: Path):
        """A UC function body is validated at CREATE time.

        Bricks deploy data -> sql_function -> job, so without this DDL the
        functions would be created against tables that do not exist yet
        and the first deploy of a fresh workspace would fail.
        """
        tables = instance_dir / "data_domains" / "data_universe" / "tables"
        declared = {p.name for p in tables.glob("*.sql")}

        assert declared == {"01_landscape_views.sql", "02_landscape_columns.sql"}

    def test_landscape_ddl_preserves_job_output(self, instance_dir: Path):
        """CREATE OR REPLACE would empty the tables on every deploy and
        silently drop the universe out of the prompt until the next run."""
        tables = instance_dir / "data_domains" / "data_universe" / "tables"

        for ddl in tables.glob("*.sql"):
            statements = "\n".join(
                line
                for line in ddl.read_text().upper().splitlines()
                if not line.lstrip().startswith("--")
            )
            assert "CREATE TABLE IF NOT EXISTS" in statements, ddl.name
            assert "CREATE OR REPLACE" not in statements, ddl.name

    def test_landscape_tables_live_in_the_agent_schema(self, instance_dir: Path):
        """The functions that read them resolve against the agent schema."""
        domain = yaml.safe_load(
            (instance_dir / "data_domains" / "data_universe" / "domain.yml").read_text()
        )

        assert domain["domain"]["schema"] == "${var.schema_prefix}_agent"

    def test_both_tiers_are_deployed(self, instance_dir: Path):
        functions = instance_dir / "sql_functions" / "data_universe" / "functions"
        deployed = {p.name for p in functions.glob("*.sql")}

        assert deployed == {"01_get_data_landscape.sql", "02_get_table_metadata.sql"}

    def test_only_tier_two_is_exposed_as_a_tool(self, instance_dir: Path):
        """Tier 1 is injected into the prompt, so offering it as a tool
        would invite the agent to fetch what it already has."""
        server = yaml.safe_load(
            (
                instance_dir
                / "payloads"
                / "agent"
                / "mcp_servers"
                / "catalog_functions"
                / "server.yml"
            ).read_text()
        )
        exposed = {entry["expose_as"] for entry in server["tools"]["allow"]}

        assert "get_table_metadata" in exposed
        assert "get_data_landscape" not in exposed

    def test_payload_enables_injection(self, instance_dir: Path):
        payload = yaml.safe_load(
            (instance_dir / "payloads" / "agent" / "payload.yml").read_text()
        )
        universe = payload["payload"]["spec"]["runtime"]["context"]["data_universe"]

        assert universe["enabled"] is True
        assert universe["function"] == "get_data_landscape"

    def test_caching_is_authored_alongside_eager_skills(self, instance_dir: Path):
        """The example must show the combination that is actually cheap.

        Caching plus lazy loading is the expensive pairing -- lazy skill
        bodies are re-written at full price behind the cached prefix -- and
        a deploy would override it to eager with a warning anyway.
        """
        spec = yaml.safe_load(
            (instance_dir / "payloads" / "agent" / "payload.yml").read_text()
        )["payload"]["spec"]

        assert spec["runtime"]["context"]["prompt_cache"]["enabled"] is True
        assert spec["runtime"]["tools"]["skills"]["loading"] == "eager"

    def test_skills_come_from_the_repository(self, instance_dir: Path):
        from payloads.agent.config import load_agent_spec

        skills = load_agent_spec(instance_dir).runtime.tools.skills
        assert skills.repository_enabled()
        assert skills.deprecated_fields() == []
        assert skills.effective_gateway() is None

    def test_prompt_does_not_ask_for_a_tier_one_call(self, instance_dir: Path):
        """The single most likely way to lose the whole benefit.

        Under injection the prompt must stop telling the agent to call
        get_data_landscape, or it will spend the tool call anyway.
        """
        payload = yaml.safe_load(
            (instance_dir / "payloads" / "agent" / "payload.yml").read_text()
        )
        prompt = payload["payload"]["spec"]["runtime"]["system_prompt"]

        assert "get_data_landscape" not in prompt
        assert "get_table_metadata" in prompt
