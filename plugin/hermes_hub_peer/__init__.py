"""hermes-hub-peer — native Hermes plugin (W3, M2).

Registers the six ``peer_*`` model tools so a normal Hermes conversation can
say *"please check with Olive and see if she can access this."* (V1).

**Why a plugin and not a drop-in tool module.** ``tools/registry.py``'s
``discover_builtin_tools()`` only globs the Hermes core's own ``tools/``
directory, so a module living in hermes-hub is never found. hermes-peer
worked around that with a ``.pth`` into venv site-packages plus a core shim
patch — which modifies Hermes core. That approach is obsolete here:
``PluginContext.register_tool()`` delegates to ``tools.registry.register()``
and user plugins load from ``~/.hermes/plugins/<name>/``, so **Hermes core is
never touched and nothing is installed into the runtime venv**.

**Import path.** This package deliberately does not require ``hermes_hub``
to be pip-installed into the Hermes runtime venv (installing into the live
runtime is exactly what this design avoids). The plugin directory lives
inside the hermes-hub checkout, so the repo root is resolved from
``__file__`` and prepended to ``sys.path`` only if ``hermes_hub`` is not
already importable. ``HERMES_HUB_REPO`` overrides the location for an
install that symlinks or copies this directory elsewhere.

**Cache safety (V3).** ``register()`` performs no network I/O and injects
nothing into the system prompt. Peer capabilities are fetched on demand by
an explicit ``peer_list``/``peer_info`` call. Injecting live peer state
would invalidate per-conversation prompt caching for every open session on
every spoke connect/disconnect — which Hermes treats as sacred.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("hermes_hub_peer")

_SKILL_MD = Path(__file__).resolve().parent / "SKILL.md"


def _repo_root() -> Path:
    """Locate the hermes-hub checkout that provides the ``hermes_hub`` package.

    ``HERMES_HUB_REPO`` wins; otherwise assume this file sits at
    ``<repo>/plugin/hermes_hub_peer/__init__.py``.
    """
    override = os.environ.get("HERMES_HUB_REPO", "")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _ensure_hermes_hub_importable() -> None:
    try:
        import hermes_hub  # noqa: F401

        return
    except ImportError:
        pass
    root = _repo_root()
    if (root / "hermes_hub" / "__init__.py").exists():
        sys.path.insert(0, str(root))
    else:
        raise ImportError(
            "cannot locate the hermes_hub package. Set HERMES_HUB_REPO to the "
            f"hermes-hub checkout (looked in {root})."
        )


def register(ctx) -> None:
    """Entry point Hermes calls at startup.

    Must be named ``register`` — Hermes looks it up by name, and a plugin
    exporting ``setup`` instead loads silently with no tools.
    """
    _ensure_hermes_hub_importable()

    from hermes_hub.tools import peer_tools

    for spec in peer_tools.TOOL_SPECS:
        ctx.register_tool(
            name=spec.name,
            toolset=peer_tools.TOOLSET_NAME,
            schema=spec.schema,
            handler=spec.handler,
            # Task 2.1: hide the tools entirely when no hub is configured,
            # mirroring hermes-peer's ``_enabled``. Cheap and static — it
            # reads env/config only, never the network and never live peer
            # state (V3).
            check_fn=peer_tools.hub_configured,
            description=spec.description,
        )

    if _SKILL_MD.exists():
        ctx.register_skill("hermes-hub-peer", _SKILL_MD)

    logger.debug(
        "hermes-hub-peer registered %d tools in toolset %s",
        len(peer_tools.TOOL_SPECS),
        peer_tools.TOOLSET_NAME,
    )
