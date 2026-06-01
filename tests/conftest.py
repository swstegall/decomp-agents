# decomp-agents — parallel autonomous Claude agents for the meteor-decomp matching workflow
# Copyright (C) 2026  Samuel Stegall
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pytest fixtures.

The package is a src-layout; ``pythonpath = ["src"]`` in pyproject.toml's
``[tool.pytest.ini_options]`` makes ``import decomp_agents`` resolve under a
bare ``python3 -m pytest`` (no editable install needed).

The autouse ``_isolate_dotenv`` fixture below neutralises the developer's
on-disk ``.env`` so config tests are HERMETIC. Without it, ``config.load_config``
calls ``_load_dotenv(<repo>/.env)`` which re-injects whatever the developer has
locally (e.g. ``DECOMP_WORKER_MODEL=opus``) AFTER a test's ``monkeypatch.delenv``
ran — silently overriding the model-default assertions. Stubbing ``_load_dotenv``
to a no-op means tests see only the environment they set via ``monkeypatch``,
matching the behaviour on a clean checkout with no ``.env`` present.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    """Stop config.load_config from reading the developer's local .env file.

    Tests drive configuration purely via ``monkeypatch.setenv`` /
    ``delenv``; the on-disk ``.env`` is a developer convenience that must
    not leak into the suite. Patching the loader to a no-op is the most
    robust isolation — it covers every ``DECOMP_*`` (and ``ANTHROPIC_*``)
    key at once rather than enumerating them.
    """
    try:
        from decomp_agents import config
    except Exception:  # pragma: no cover — package not importable yet
        return
    monkeypatch.setattr(config, "_load_dotenv", lambda *a, **k: None)
