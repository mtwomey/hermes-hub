"""Tests for the native Hermes plugin packaging (W3 M2).

The plugin lives in ``plugin/hermes_hub_peer/`` and registers the six
``peer_*`` tools through ``PluginContext.register_tool()``. Hermes core is
never patched and no ``.pth`` file is written — that was hermes-peer's
approach and it is obsolete here.

These tests do NOT require the Hermes runtime: they drive ``register(ctx)``
with a fake context that records what was registered. The genuine
runtime-load proof (real Hermes plugin manager, isolated ``HERMES_HOME`` in
a subprocess) is ``scripts/w3_gate2_isolated_load.py``, run for Gate 2.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin" / "hermes_hub_peer"

EXPECTED_TOOLS = [
    "peer_list",
    "peer_info",
    "peer_discover",
    "peer_ask",
    "peer_status",
    "peer_fetch_artifact",
]


class FakePluginContext:
    """Records what a plugin registers, mirroring PluginContext's signature."""

    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}
        self.skills: dict[str, Path] = {}
        self.system_prompt_additions: list[str] = []

    def register_tool(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler,
        check_fn=None,
        requires_env=None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        override: bool = False,
    ):
        self.tools[name] = {
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "check_fn": check_fn,
            "description": description,
            "override": override,
        }
        return None

    def register_skill(self, name: str, path: Path):
        self.skills[name] = path


def load_plugin_module(module_name: str = "hermes_plugins.w3_test_hub_peer"):
    """Import the plugin exactly the way Hermes's PluginManager does:
    as a package under ``hermes_plugins.<slug>`` with
    ``submodule_search_locations`` set — NOT by putting the repo on sys.path.
    """
    ns_parent = "hermes_plugins"
    if ns_parent not in sys.modules:
        pkg = types.ModuleType(ns_parent)
        pkg.__path__ = []  # type: ignore[attr-defined]
        pkg.__package__ = ns_parent
        sys.modules[ns_parent] = pkg

    for stale in [n for n in list(sys.modules) if n == module_name or n.startswith(module_name + ".")]:
        del sys.modules[stale]

    spec = importlib.util.spec_from_file_location(
        module_name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# -- manifest ----------------------------------------------------------------


def test_manifest_exists_and_declares_every_tool():
    import yaml

    manifest_path = PLUGIN_DIR / "plugin.yaml"
    assert manifest_path.exists(), "plugin.yaml is the manifest format Hermes requires"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert data["name"] == "hermes-hub-peer"
    assert data["version"]
    assert data["description"].strip()
    assert data["provides_tools"] == EXPECTED_TOOLS


def test_manifest_uses_only_fields_hermes_understands():
    """Unknown manifest fields warn at load. Keep the manifest clean."""
    import yaml

    known = {
        "name", "version", "description", "author", "requires_env",
        "provides_tools", "provides_hooks", "kind", "hooks", "label",
        "optional_env", "platforms", "external_dependencies", "pip_dependencies",
        "provides_browser_providers", "provides_web_providers",
        "manifest_version", "api_version", "requires_plugins",
        "python_dependencies", "config_schema", "license", "homepage", "tags",
        "capabilities", "emits", "listens", "hermes", "depends",
    }
    data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    assert set(data) <= known, f"unknown manifest fields: {set(data) - known}"


def test_plugin_has_an_init_with_a_register_entry_point():
    """Hermes looks for ``register`` by name; ``setup`` silently registers nothing."""
    module = load_plugin_module()
    assert hasattr(module, "register")
    assert callable(module.register)


def test_readme_documents_install_uninstall_and_config():
    readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    for key in (
        "HERMES_HUB_URL",
        "HERMES_HUB_TOKEN",
        "HERMES_HUB_PEER_CREDENTIAL_",
        "hermes-hub",
    ):
        assert key in readme, f"README must document {key}"
    lowered = readme.lower()
    assert "uninstall" in lowered
    assert "rollback" in lowered


# -- registration ------------------------------------------------------------


def test_register_registers_all_six_tools_via_the_plugin_api():
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    assert sorted(ctx.tools) == sorted(EXPECTED_TOOLS)


def test_every_registered_tool_passes_name_and_toolset_explicitly():
    """Omitting either is the #1 silent plugin failure mode."""
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    for name, entry in ctx.tools.items():
        assert entry["name"] == name
        assert entry["toolset"], f"{name} registered without a toolset"
        assert entry["schema"]["name"] == name


def test_no_tool_is_registered_with_override():
    """The peer_* names are not built-ins; override would be a red flag."""
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    assert all(entry["override"] is False for entry in ctx.tools.values())


def test_every_tool_has_a_check_fn_so_it_hides_without_a_hub():
    """Task 2.1, mirroring hermes-peer's ``_enabled`` pattern."""
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    for name, entry in ctx.tools.items():
        assert callable(entry["check_fn"]), f"{name} has no check_fn"


def test_check_fn_is_false_without_a_hub_and_true_with_one(monkeypatch):
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    check = ctx.tools["peer_ask"]["check_fn"]
    from hermes_hub.tools import peer_tools

    monkeypatch.delenv("HERMES_HUB_URL", raising=False)
    monkeypatch.setattr(peer_tools, "_configured_hub_url_from_config", lambda: "")
    assert check() is False
    monkeypatch.setenv("HERMES_HUB_URL", "http://127.0.0.1:8770")
    assert check() is True


def test_registered_handlers_return_json_strings():
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    out = ctx.tools["peer_list"]["handler"]({"hub_url": "http://127.0.0.1:1"})
    assert isinstance(out, str)
    assert json.loads(out)["success"] is False


def test_registered_handler_accepts_hermes_style_kwargs():
    """Hermes calls handlers as ``handler(args, **kwargs)``."""
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    out = ctx.tools["peer_list"]["handler"](
        {"hub_url": "http://127.0.0.1:1"}, session_id="s1", platform="gateway"
    )
    assert json.loads(out)["success"] is False


def test_registered_handlers_are_the_real_m1_functions():
    """The plugin must delegate to M1, not re-implement anything."""
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    from hermes_hub.tools import peer_tools

    for spec in peer_tools.TOOL_SPECS:
        assert ctx.tools[spec.name]["schema"] is spec.schema


# -- V3 cache safety ---------------------------------------------------------


def test_plugin_does_not_inject_live_peer_state_into_the_system_prompt():
    """V3 / hard stop: injecting per-spoke content would invalidate the
    prompt cache for every open session on every spoke connect/disconnect.

    Verified two ways: (1) the plugin never calls any prompt-injection
    surface on the context, and (2) registration performs no network I/O.
    """
    ctx = FakePluginContext()

    calls: list[str] = []

    class TrackingContext(FakePluginContext):
        def __getattr__(self, item):  # only called for MISSING attributes
            calls.append(item)
            raise AttributeError(item)

    tracking = TrackingContext()
    load_plugin_module().register(tracking)
    forbidden = {
        "register_system_prompt",
        "add_system_prompt",
        "extend_system_prompt",
        "register_prompt_section",
        "register_context_provider",
        "inject_message",
    }
    assert not (set(calls) & forbidden), f"plugin touched prompt surface: {calls}"
    assert tracking.system_prompt_additions == []


def test_registration_performs_no_network_io(monkeypatch):
    """V3: peer capabilities are fetched on demand by an explicit tool call,
    never at load time. A network call during register() would both break
    cache-safety intent and make gateway startup depend on the hub."""
    import socket

    def explode(*args, **kwargs):
        raise AssertionError("plugin registration performed network I/O")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    assert len(ctx.tools) == 6


def test_any_bundled_skill_is_static_and_names_no_specific_peer():
    """A short static line is acceptable under V3; per-spoke content is not.
    Whatever the plugin ships as a skill must not enumerate live peers."""
    ctx = FakePluginContext()
    load_plugin_module().register(ctx)
    for _name, path in ctx.skills.items():
        text = Path(path).read_text(encoding="utf-8")
        assert "Currently connected" not in text
        # The document must tell the model to CALL peer_list rather than
        # embedding an answer.
        assert "peer_list" in text
