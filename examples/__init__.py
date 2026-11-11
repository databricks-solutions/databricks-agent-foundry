"""Named end-to-end app compositions for ``agent-foundry init``.

Each subdirectory under :mod:`examples` is one self-contained
*variant* the init wizard can drop into a freshly generated repo --
e.g. ``examples/portfolio_assistant/`` ships the reference agent with
Genie analytics + Vector Search docs retrieval.

This package exists purely so :func:`importlib.resources.files` can
discover variant trees in both source checkouts and built wheels.
Nothing in the framework imports from it; the renderer
(:mod:`agent_foundry.init.renderer`) copies a chosen variant's
contents byte-for-byte into the generated repo's ``instances/``
directory.

Adding a new variant
--------------------

1. Create ``examples/<variant>/`` with the same per-instance shape
   as an existing variant (``foundry.toml`` + per-brick subdirs).
2. Add ``examples/<variant>/__init__.py`` so packaging picks it up.
3. Add ``examples/<variant>/manifest.toml`` with ``display_name``
   and ``description`` -- the init wizard discovers variants by
   scanning these manifests; no framework code edits required.
"""
