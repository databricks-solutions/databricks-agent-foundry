# bricks/ai_gateway/

> **Technical reference** for deployers and developers.

Deploys git-managed [Unity Gateway](https://docs.databricks.com/aws/en/ai-gateway/) artifacts. Each artifact kind is one optional section of `config.yml`, one subfolder and one script. v1 ships **skills**: every folder under `ai_gateway/skills/` becomes a Unity Catalog skill in the configured schema.

The brick only deploys. An agent reads the skills only when its `payload.yml` references them, either as pinned FQNs or through a discovery location. Nothing is pinned automatically.

## Layout

```
instances/<app>/ai_gateway/
├── config.yml
└── skills/
    └── <skill_id>/          # folder name = skill id = frontmatter name
        ├── SKILL.md
        └── ...              # optional supporting files
```

```yaml
# config.yml -- ${var.*} placeholders are substituted before validation
skills:
  schema: ${var.catalog}.${var.schema_prefix}_skills
```

`SKILL.md` needs [agentskills.io](https://agentskills.io) frontmatter: a `name` equal to the folder name (lowercase letters, digits and single hyphens, at most 64 characters) and a non-empty `description` of at most 1024 characters. `SKILL.md` is capped at 20,000 characters. Dotfiles are not deployed.

## Deploy (git wins)

The deploying principal creates the schema when it is missing and owns every skill it creates. For each folder, deploy:

1. Validates every folder first and writes nothing if any of them is invalid.
2. Creates the skill when it is missing.
3. Skips the skill when its remote etag and its local content hash both match the recorded state. Nothing is uploaded and the etag stays the same, so running agents keep their prompt cache.
4. Otherwise downloads the remote bundle. If it equals git, only the recorded etag is refreshed. If it differs, deploy uploads every file, deletes remote-only files and finalizes the skill.

Remote edits made since the last deploy (for example in the workspace UI) are overwritten. Deploy prints them as `OVERWROTE remote edits` and lists them under `overwritten`, along with any removed remote-only files, so nothing is lost silently. To keep a UI edit, import it first.

Skills the brick created whose folder is gone from git, or whose schema changed, are deleted. Skills the brick did not create are never touched.

```bash
uv run agent-foundry brick ai_gateway deploy --instance my-app --target dev [--dry-run]
```

## Import UI edits back into git

```bash
uv run agent-foundry brick ai_gateway import --instance my-app --target dev [--skill triage ...]
```

Import downloads each skill (by default every skill in the schema) into `ai_gateway/skills/<skill_id>/` and replaces the local folder, so it mirrors the remote bundle. It checks the bundle against the same frontmatter rules and refuses paths that would leave the skill folder. It writes no state: the next deploy sees remote content equal to git, uploads nothing and only refreshes the recorded etag. Review the result with `git diff` and commit it.

## State and destroy

The workspace state (brick type `ai_gateway_skills`) records the schema, the schemas the brick created, and each skill's `etag` and `content_hash`. `destroy` deletes the skills in that state, then drops each schema the brick created with a non-forced delete. Unity Catalog refuses that delete while anything is left in the schema, so a schema that still has content is preserved. Skill bundle data is deleted asynchronously, so a schema can stay behind until a later destroy.

## Consuming the skills

Reference the brick schema in the agent payload's `runtime.tools.skills.unity_catalog`, using the same variables. Repository skills from `skills/` may stay on next to it:

```yaml
skills:
  unity_catalog:
    pinned:
      - fqn: ${var.catalog}.${var.schema_prefix}_skills.triage
        identity: sp
```

For pinned `sp` skills and `sp` discovery locations, the agent payload grants the App service principal read access (`READ_VOLUME`, plus `USE CATALOG` / `USE SCHEMA`) after the App is reconciled.
