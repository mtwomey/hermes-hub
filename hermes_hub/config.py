"""Spoke identity configuration (Task 1.4, V10).

The local spoke stays named "Pumpkin" -- this host IS Pumpkin, and the name
agreeing with core `hermes peer`'s ``peer.local.name`` is correct, not a
collision (see docs/VISION.md V10, plan Task 1.4). This module exists only
to make the name a config value instead of a literal scattered across
scripts, so a future second profile or a renamed host is a config edit --
NOT to rename anything on this machine. The default stays "Pumpkin".

Resolution order, first hit wins: explicit argument > env var
``HERMES_HUB_SPOKE_NAME`` > built-in default "Pumpkin".
"""

from __future__ import annotations

import os

ENV_SPOKE_NAME = "HERMES_HUB_SPOKE_NAME"
DEFAULT_SPOKE_NAME = "Pumpkin"


def resolve_spoke_name(explicit: str = "") -> str:
    """Resolve the local spoke's name. Defaults to "Pumpkin"; never renames
    anything by itself -- callers decide whether to override."""
    if explicit:
        return explicit
    env_value = os.environ.get(ENV_SPOKE_NAME, "")
    if env_value:
        return env_value
    return DEFAULT_SPOKE_NAME
