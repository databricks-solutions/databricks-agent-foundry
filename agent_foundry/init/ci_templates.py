"""CI templates for ``agent-foundry init`` (issue #210).

The init wizard collects exactly one CI provider (``git`` =
GitHub Actions or ``gitlab`` = GitLab CI) and the renderer writes
that provider's files only.

CI flow (both providers implement the same shape)
-------------------------------------------------

Three pipeline triggers, keyed off branch:

* PR ``feature/*`` -> ``main``: unit / offline tests only (no workspace).
* PR ``main`` -> ``release``: cheap checks, then a protected ``staging``
  environment approval, then deploy + post-deploy acceptance. Staging
  is a persistent application environment and is **not** destroyed.
* Merge to ``release``: protected ``prod`` environment approval, then
  deploy to prod. Production is never destroyed.

The ``dev`` target is laptop-only; CI never touches it.

Auth contract (OAuth service-principal M2M)
-------------------------------------------

Per target, CI must expose three secrets:

* ``DATABRICKS_HOST_<TARGET>``
* ``DATABRICKS_CLIENT_ID_<TARGET>``
* ``DATABRICKS_CLIENT_SECRET_<TARGET>``

Templates surface these as job-level environment variables; the
Databricks SDK auto-detects them.  No auth code is generated.
"""

from __future__ import annotations

from agent_foundry.init.wizard import InitContext

# ---------------------------------------------------------------------------
# GitHub Actions
# ---------------------------------------------------------------------------

_GH_PR_TO_MAIN = """\
name: pr-to-main

# Unit tests on every PR targeting ``main`` from a feature branch.
# No workspace credentials needed.

on:
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install 3.11

      - name: Optional GitHub auth when the framework pin is a private fork
        # Only needed when the framework git pin is not publicly cloneable.
        run: |
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "git@github.com:"
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "ssh://git@github.com/"

      - name: Install project
        run: uv sync --frozen

      - name: Lint
        run: uv run --no-sync ruff check .

      - name: Unit tests
        run: uv run --no-sync pytest -q -m "not live and not component"
"""


_GH_PR_TO_RELEASE = """\
name: pr-to-release

# Cheap checks first. Live staging deploy waits for the protected
# ``staging`` GitHub Environment (required reviewers). Staging is a
# persistent application environment: deploy, wait, accept, keep.

on:
  pull_request:
    branches: [release]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install 3.11

      - name: Optional GitHub auth when the framework pin is a private fork
        run: |
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "git@github.com:"
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "ssh://git@github.com/"

      - name: Install project
        run: uv sync --frozen

      - name: Lint
        run: uv run --no-sync ruff check .

      - name: Unit tests
        run: uv run --no-sync pytest -q -m "not live and not component"

  deploy-staging:
    needs: unit-tests
    runs-on: ubuntu-latest
    timeout-minutes: 60
    environment:
      name: staging
    # Client secret (OAuth M2M). For GitHub OIDC instead, see docs/how-to/deploy-cicd.md
    # (grant id-token: write, set DATABRICKS_AUTH_TYPE: github-oidc, drop DATABRICKS_CLIENT_SECRET).
    env:
      DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST_STAGING }}
      DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID_STAGING }}
      DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET_STAGING }}
      AGENT_FOUNDRY_TEST_CATALOG: ${{ vars.AGENT_FOUNDRY_TEST_CATALOG }}
      TEST_WAREHOUSE_ID: ${{ vars.TEST_WAREHOUSE_ID }}
      AGENT_FOUNDRY_TARGET: staging
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install 3.11

      - uses: databricks/setup-cli@v1.19.0

      - name: Optional GitHub auth when the framework pin is a private fork
        run: |
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "git@github.com:"
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "ssh://git@github.com/"

      - name: Install project
        run: uv sync --frozen

      - name: Deploy to staging
        run: uv run --no-sync agent-foundry deploy --instance __PROJECT_NAME__ --target staging

      - name: Wait for App
        run: uv run --no-sync agent-foundry wait --instance __PROJECT_NAME__ --target staging

      - name: Post-deploy live checks
        run: uv run --no-sync pytest -q -m live --run-live
"""


_GH_RELEASE_DEPLOY_PROD = """\
name: release-deploy-prod

# Deploy to prod on merge to ``release``, gated by manual approval via the
# ``prod`` GitHub Environment (configure it in repo Settings -> Environments
# with required reviewers before relying on the gate).

on:
  push:
    branches: [release]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  deploy-prod:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    environment:
      name: prod
    # Client secret (OAuth M2M). For GitHub OIDC instead, see docs/how-to/deploy-cicd.md
    # (grant id-token: write, set DATABRICKS_AUTH_TYPE: github-oidc, drop DATABRICKS_CLIENT_SECRET).
    env:
      DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST_PROD }}
      DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID_PROD }}
      DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET_PROD }}
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - name: Set up Python
        run: uv python install 3.11

      - uses: databricks/setup-cli@v1.19.0

      - name: Optional GitHub auth when the framework pin is a private fork
        run: |
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "git@github.com:"
          git config --global url."https://${{ secrets.FRAMEWORK_REPO_TOKEN }}@github.com/".insteadOf "ssh://git@github.com/"

      - name: Install project
        run: uv sync --frozen

      - name: Deploy to prod
        run: uv run --no-sync agent-foundry deploy --instance __PROJECT_NAME__ --target prod
"""


# ---------------------------------------------------------------------------
# GitLab CI (single file, branch-rule keyed stages)
# ---------------------------------------------------------------------------

_GITLAB_CI = """\
# GitLab CI for __PROJECT_NAME__, generated by ``agent-foundry init``.
#
# Three-stage flow keyed off the source/target branch:
#
#   * MR feature/* -> main:    unit / offline tests only.
#   * MR main      -> release: cheap checks, then manual staging deploy
#     + post-deploy acceptance. Staging is persistent (never destroyed).
#   * Merge to     release:    deploy: prod (manual gate). Never destroyed.
#
# CI secrets (set under Settings -> CI/CD -> Variables, masked):
#
#   * DATABRICKS_HOST_STAGING / DATABRICKS_CLIENT_ID_STAGING / DATABRICKS_CLIENT_SECRET_STAGING
#   * DATABRICKS_HOST_PROD    / DATABRICKS_CLIENT_ID_PROD    / DATABRICKS_CLIENT_SECRET_PROD
#   * AGENT_FOUNDRY_TEST_CATALOG / TEST_WAREHOUSE_ID (non-secret)
#   * FRAMEWORK_REPO_TOKEN -- read-only PAT for the private agent-foundry repo

default:
  image: ghcr.io/astral-sh/uv:0.5-python3.11-bookworm-slim
  before_script:
    - git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "git@github.com:"
    - git config --global url."https://${FRAMEWORK_REPO_TOKEN}@github.com/".insteadOf "ssh://git@github.com/"
    - uv sync --frozen

stages:
  - test
  - deploy
  - accept

unit-tests:
  stage: test
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "main"'
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "release"'
  script:
    - uv run --no-sync ruff check .
    - uv run --no-sync pytest -q -m "not live and not component"

deploy-staging:
  stage: deploy
  environment:
    name: staging
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "release"'
      when: manual
  variables:
    DATABRICKS_HOST: $DATABRICKS_HOST_STAGING
    DATABRICKS_CLIENT_ID: $DATABRICKS_CLIENT_ID_STAGING
    DATABRICKS_CLIENT_SECRET: $DATABRICKS_CLIENT_SECRET_STAGING
    AGENT_FOUNDRY_TARGET: staging
  script:
    - uv run --no-sync agent-foundry deploy --instance __PROJECT_NAME__ --target staging
    - uv run --no-sync agent-foundry wait --instance __PROJECT_NAME__ --target staging

acceptance-staging:
  stage: accept
  environment:
    name: staging
  needs: ["deploy-staging"]
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event" && $CI_MERGE_REQUEST_TARGET_BRANCH_NAME == "release"'
  variables:
    DATABRICKS_HOST: $DATABRICKS_HOST_STAGING
    DATABRICKS_CLIENT_ID: $DATABRICKS_CLIENT_ID_STAGING
    DATABRICKS_CLIENT_SECRET: $DATABRICKS_CLIENT_SECRET_STAGING
    AGENT_FOUNDRY_TARGET: staging
  script:
    - uv run --no-sync pytest -q -m live --run-live

deploy-prod:
  stage: deploy
  environment:
    name: prod
  rules:
    - if: '$CI_COMMIT_BRANCH == "release"'
      when: manual
  variables:
    DATABRICKS_HOST: $DATABRICKS_HOST_PROD
    DATABRICKS_CLIENT_ID: $DATABRICKS_CLIENT_ID_PROD
    DATABRICKS_CLIENT_SECRET: $DATABRICKS_CLIENT_SECRET_PROD
  script:
    - uv run --no-sync agent-foundry deploy --instance __PROJECT_NAME__ --target prod
"""


# ---------------------------------------------------------------------------
# Public renderers
# ---------------------------------------------------------------------------


def render_github_workflows(ctx: InitContext) -> dict[str, str]:
    """Return ``{filename: contents}`` for the three GitHub Actions workflows.

    Keys are workflow filenames relative to ``.github/workflows/``.
    The renderer (``renderer.render``) writes them under that
    directory in the generated repo.

    Substitution strategy: ``.format()`` is unsafe here because the
    GitHub Actions expression syntax uses ``${{ ... }}`` which collides
    with Python's format-spec curly braces.  We use a literal sentinel
    (``__PROJECT_NAME__``) and ``str.replace`` instead.
    """
    return {
        "pr-to-main.yml": _GH_PR_TO_MAIN,
        "pr-to-release.yml": _GH_PR_TO_RELEASE.replace(
            "__PROJECT_NAME__",
            ctx.project_name,
        ),
        "release-deploy-prod.yml": _GH_RELEASE_DEPLOY_PROD.replace(
            "__PROJECT_NAME__",
            ctx.project_name,
        ),
    }


def render_gitlab_ci(ctx: InitContext) -> str:
    """Return the contents of ``.gitlab-ci.yml``.

    Uses the same ``__PROJECT_NAME__`` sentinel + ``str.replace`` strategy
    as the GitHub workflows so that ``${VAR}`` references survive intact.
    """
    return _GITLAB_CI.replace("__PROJECT_NAME__", ctx.project_name)


__all__ = ["render_github_workflows", "render_gitlab_ci"]
