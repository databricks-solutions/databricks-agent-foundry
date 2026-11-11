"""Composable Agent Brick implementations.

Each subpackage under ``bricks/`` is a self-contained brick that exposes a
:class:`agent_foundry.Brick` subclass plus the templates, scripts, and
state files it owns. The deploy orchestrator iterates the registry exposed
by :func:`agent_foundry.iter_bricks` -- there is no central import list
here. See ``bricks/README.md`` for the full brick catalog.
"""
