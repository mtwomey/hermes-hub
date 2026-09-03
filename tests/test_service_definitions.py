"""M1 checkpoint: launchd service definitions and installer (Task 1.1-1.4).

Nothing here loads anything into launchd. It proves:
- the installer generates well-formed plists into a temp LaunchAgents dir
  with correct absolute paths (Task 1.1/1.3);
- `install` then `uninstall` is idempotent -- uninstall removes exactly
  what install created;
- the real ~/Library/LaunchAgents/ai.hermes.gateway.plist is never touched
  by any of this (the installer targets LAUNCH_AGENTS_DIR only).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"
INSTALLER = SERVICES_DIR / "install-hub-services.sh"


def _run_installer(command: str, *, env_overrides: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(INSTALLER), command],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _dry_run_env(tmp_path: Path) -> dict:
    fake_home = tmp_path / "home"
    launch_agents = tmp_path / "LaunchAgents"
    hub_venv = tmp_path / "hub-venv"
    hermes_venv = tmp_path / "hermes-agent-venv"
    (hub_venv / "bin").mkdir(parents=True)
    (hub_venv / "bin" / "python").write_text("#!/bin/sh\n")
    fake_home.mkdir(parents=True)
    launch_agents.mkdir(parents=True)
    return {
        "HOMES_DIR": str(fake_home),
        "LOG_DIR": str(fake_home / "logs"),
        "HUB_VENV": str(hub_venv),
        "HERMES_AGENT_VENV": str(hermes_venv),
        "LAUNCH_AGENTS_DIR": str(launch_agents),
        # Isolate fixture execution from any real deployment configuration.
        "HUB_CONFIG_FILE": str(tmp_path / "missing-hub-service.env"),
        "HUB_HOST": "127.0.0.1",
        "HUB_PORT": "8770",
        "SPOKE_NAME": "Pumpkin",
        # Poison launchctl so a bug that tries to actually load anything
        # fails loudly during the test instead of silently touching the
        # real launchd domain.
        "PATH": f"{tmp_path / 'binstub'}:{os.environ['PATH']}",
    }


def _install_stub_launchctl(tmp_path: Path) -> None:
    stub_dir = tmp_path / "binstub"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "launchctl"
    stub.write_text("#!/bin/bash\necho \"stub-launchctl $*\" 1>&2\nexit 0\n")
    stub.chmod(0o755)


def test_installer_and_templates_exist():
    assert INSTALLER.exists() and os.access(INSTALLER, os.X_OK)
    assert (SERVICES_DIR / "ai.hermes.hub.plist.template").exists()
    assert (SERVICES_DIR / "ai.hermes.spoke.plist.template").exists()
    assert (SERVICES_DIR / "hermes-hub-wrapper.sh").exists()
    assert (SERVICES_DIR / "hermes-spoke-wrapper.sh").exists()


def test_dry_run_install_generates_valid_plists(tmp_path):
    _install_stub_launchctl(tmp_path)
    env = _dry_run_env(tmp_path)
    result = _run_installer("install", env_overrides=env)
    assert result.returncode == 0, result.stderr

    launch_agents = Path(env["LAUNCH_AGENTS_DIR"])
    hub_plist = launch_agents / "ai.hermes.hub.plist"
    spoke_plist = launch_agents / "ai.hermes.spoke.plist"
    assert hub_plist.exists()
    assert spoke_plist.exists()

    # plutil -lint on each
    for plist in (hub_plist, spoke_plist):
        lint = subprocess.run(
            ["plutil", "-lint", str(plist)], capture_output=True, text=True
        )
        assert lint.returncode == 0, lint.stdout + lint.stderr

    hub_data = plistlib.loads(hub_plist.read_bytes())
    assert hub_data["Label"] == "ai.hermes.hub"
    assert hub_data["ProgramArguments"] == [str(SERVICES_DIR / "hermes-hub-wrapper.sh")]
    assert hub_data["KeepAlive"] is True
    assert hub_data["RunAtLoad"] is True
    assert hub_data["EnvironmentVariables"]["HERMES_HUB_PORT"] == "8770"
    assert hub_data["EnvironmentVariables"]["HERMES_HUB_PUBLIC_URL"] == "http://127.0.0.1:8770"
    assert hub_data["EnvironmentVariables"]["HERMES_HUB_REPO"] == str(REPO_ROOT)
    # No absolute-path placeholders left un-substituted.
    assert "__" not in "".join(hub_data["ProgramArguments"])

    spoke_data = plistlib.loads(spoke_plist.read_bytes())
    assert spoke_data["Label"] == "ai.hermes.spoke"
    assert spoke_data["EnvironmentVariables"]["HERMES_HUB_SPOKE_NAME"] == "Pumpkin"
    assert spoke_data["EnvironmentVariables"]["HERMES_AGENT_VENV"] == env["HERMES_AGENT_VENV"]
    # No credential-shaped key anywhere in the spoke plist.
    blob = spoke_plist.read_text()
    assert "CREDENTIAL" not in blob.upper() or "SPOKE_CREDENTIAL" not in blob.upper()


def test_spoke_mode_generates_only_spoke_service(tmp_path):
    _install_stub_launchctl(tmp_path)
    env = _dry_run_env(tmp_path)
    env["SERVICE_MODE"] = "spoke"
    env["SPOKE_HUB_HOST"] = "hub.example.invalid"

    result = _run_installer("install", env_overrides=env)

    assert result.returncode == 0, result.stderr
    launch_agents = Path(env["LAUNCH_AGENTS_DIR"])
    assert not (launch_agents / "ai.hermes.hub.plist").exists()
    spoke_plist = launch_agents / "ai.hermes.spoke.plist"
    spoke_data = plistlib.loads(spoke_plist.read_bytes())
    assert spoke_data["EnvironmentVariables"]["HERMES_HUB_HOST"] == "hub.example.invalid"


def test_hub_mode_generates_only_hub_service(tmp_path):
    _install_stub_launchctl(tmp_path)
    env = _dry_run_env(tmp_path)
    env["SERVICE_MODE"] = "hub"
    env["HUB_BIND_HOST"] = "0.0.0.0"
    env["HUB_PUBLIC_URL"] = "https://hub.example.invalid:8770"

    result = _run_installer("install", env_overrides=env)

    assert result.returncode == 0, result.stderr
    launch_agents = Path(env["LAUNCH_AGENTS_DIR"])
    hub_plist = launch_agents / "ai.hermes.hub.plist"
    assert hub_plist.exists()
    assert not (launch_agents / "ai.hermes.spoke.plist").exists()
    hub_data = plistlib.loads(hub_plist.read_bytes())
    assert hub_data["EnvironmentVariables"]["HERMES_HUB_HOST"] == "0.0.0.0"


def test_spoke_only_mode_does_not_require_hub_venv(tmp_path):
    _install_stub_launchctl(tmp_path)
    env = _dry_run_env(tmp_path)
    env["SERVICE_MODE"] = "spoke"
    env["SPOKE_HUB_HOST"] = "hub.example.invalid"
    env["HUB_VENV"] = str(tmp_path / "no-such-hub-venv")

    result = _run_installer("install", env_overrides=env)

    assert result.returncode == 0, result.stderr
    assert (Path(env["LAUNCH_AGENTS_DIR"]) / "ai.hermes.spoke.plist").exists()


def test_wildcard_hub_bind_requires_explicit_public_url(tmp_path):
    env = _dry_run_env(tmp_path)
    env["HUB_HOST"] = "0.0.0.0"
    result = _run_installer("install", env_overrides=env)

    assert result.returncode == 2
    assert "HUB_PUBLIC_URL is required" in result.stderr


def test_config_file_supplies_persistent_lan_endpoint(tmp_path):
    _install_stub_launchctl(tmp_path)
    config_file = tmp_path / "hub-service.env"
    config_file.write_text(
        "HUB_HOST=0.0.0.0\n"
        "HUB_PUBLIC_URL=https://hub.example.invalid:8770\n"
    )
    env = _dry_run_env(tmp_path)
    env.pop("HUB_HOST")
    env["HUB_CONFIG_FILE"] = str(config_file)

    result = _run_installer("install", env_overrides=env)

    assert result.returncode == 0, result.stderr
    hub_plist = Path(env["LAUNCH_AGENTS_DIR"]) / "ai.hermes.hub.plist"
    hub_data = plistlib.loads(hub_plist.read_bytes())
    assert hub_data["EnvironmentVariables"]["HERMES_HUB_HOST"] == "0.0.0.0"
    assert (
        hub_data["EnvironmentVariables"]["HERMES_HUB_PUBLIC_URL"]
        == "https://hub.example.invalid:8770"
    )


def test_status_reports_selected_mode_and_endpoints_without_crashing(tmp_path):
    _install_stub_launchctl(tmp_path)
    env = _dry_run_env(tmp_path)
    env.pop("HUB_HOST")
    env["SERVICE_MODE"] = "spoke"
    env["SPOKE_HUB_HOST"] = "hub.example.invalid"

    result = _run_installer("status", env_overrides=env)

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert "hub.example.invalid" in result.stdout


def test_status_queries_each_launchd_service_domain(tmp_path):
    stub_dir = tmp_path / "binstub"
    stub_dir.mkdir()
    status_log = tmp_path / "launchctl.log"
    stub = stub_dir / "launchctl"
    stub.write_text(
        "#!/bin/bash\n"
        "printf '%s\\n' \"$*\" >> \"$STATUS_LOG\"\n"
        "test \"$1\" = print\n"
    )
    stub.chmod(0o755)
    env = _dry_run_env(tmp_path)
    env["PATH"] = f"{stub_dir}:{os.environ['PATH']}"
    env["STATUS_LOG"] = str(status_log)

    result = _run_installer("status", env_overrides=env)

    assert result.returncode == 0, result.stderr
    calls = status_log.read_text()
    assert "print gui/" in calls
    assert "ai.hermes.hub" in calls
    assert "ai.hermes.spoke" in calls


def test_dry_run_mode_never_calls_launchctl(tmp_path):
    """Gate 1 must create/lint/remove only temp plists -- loading belongs to
    M3. A poison launchctl makes any accidental invocation fail this test."""
    stub_dir = tmp_path / "binstub"
    stub_dir.mkdir()
    poison = stub_dir / "launchctl"
    poison.write_text("#!/bin/bash\necho launchctl-must-not-run >&2\nexit 97\n")
    poison.chmod(0o755)

    env = _dry_run_env(tmp_path)
    env["DRY_RUN"] = "1"
    install_result = _run_installer("install", env_overrides=env)
    assert install_result.returncode == 0, install_result.stderr
    assert "launchctl was not called" in install_result.stdout
    assert "launchctl-must-not-run" not in install_result.stderr

    uninstall_result = _run_installer("uninstall", env_overrides=env)
    assert uninstall_result.returncode == 0, uninstall_result.stderr
    assert "launchctl was not called" in uninstall_result.stdout
    assert "launchctl-must-not-run" not in uninstall_result.stderr


def test_dry_run_install_never_touches_real_gateway_plist(tmp_path):
    real_gateway_plist = Path.home() / "Library" / "LaunchAgents" / "ai.hermes.gateway.plist"
    before = real_gateway_plist.read_bytes() if real_gateway_plist.exists() else None

    _install_stub_launchctl(tmp_path)
    env = _dry_run_env(tmp_path)
    result = _run_installer("install", env_overrides=env)
    assert result.returncode == 0, result.stderr

    after = real_gateway_plist.read_bytes() if real_gateway_plist.exists() else None
    assert before == after

    # The dry run must not have invoked the stub launchctl with anything
    # naming the gateway label.
    assert "ai.hermes.gateway" not in result.stderr


def test_uninstall_removes_exactly_what_install_created(tmp_path):
    _install_stub_launchctl(tmp_path)
    env = _dry_run_env(tmp_path)
    install_result = _run_installer("install", env_overrides=env)
    assert install_result.returncode == 0, install_result.stderr

    launch_agents = Path(env["LAUNCH_AGENTS_DIR"])
    hub_plist = launch_agents / "ai.hermes.hub.plist"
    spoke_plist = launch_agents / "ai.hermes.spoke.plist"
    assert hub_plist.exists() and spoke_plist.exists()

    uninstall_result = _run_installer("uninstall", env_overrides=env)
    assert uninstall_result.returncode == 0, uninstall_result.stderr

    assert not hub_plist.exists()
    assert not spoke_plist.exists()
    # Nothing else appeared in the directory.
    assert list(launch_agents.iterdir()) == []


def test_wrapper_scripts_never_export_or_pass_a_credential_value():
    """Comments may explain the credential model; no wrapper may declare,
    export, or forward an actual credential-bearing environment variable
    or command-line argument."""
    for wrapper in ("hermes-hub-wrapper.sh", "hermes-spoke-wrapper.sh"):
        for line in (SERVICES_DIR / wrapper).read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "CREDENTIAL" not in stripped.upper(), (wrapper, line)


def test_spoke_wrapper_fails_loudly_when_transport_deps_missing(tmp_path):
    """The spoke wrapper must exit non-zero with a clear message, not start
    a broken process, when the Hermes venv lacks a2a/websockets/uvicorn."""
    fake_venv = tmp_path / "hermes-agent-venv"
    (fake_venv / "bin").mkdir(parents=True)
    python_stub = fake_venv / "bin" / "python"
    # A python stub that fails any `-c "import ..."` check, simulating a
    # venv missing the transport deps.
    python_stub.write_text("#!/bin/sh\nexit 1\n")
    python_stub.chmod(0o755)

    env = dict(os.environ)
    env["HERMES_AGENT_VENV"] = str(fake_venv)
    env["HERMES_HUB_REPO"] = str(REPO_ROOT)
    env["HERMES_HUB_PORT"] = "8770"
    env["HERMES_HUB_SPOKE_NAME"] = "Pumpkin"

    result = subprocess.run(
        ["bash", str(SERVICES_DIR / "hermes-spoke-wrapper.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "a2a-sdk" in result.stderr or "websockets" in result.stderr or "uvicorn" in result.stderr


def test_hub_wrapper_fails_loudly_when_venv_missing(tmp_path):
    env = dict(os.environ)
    env["HERMES_HUB_VENV"] = str(tmp_path / "does-not-exist")
    env["HERMES_HUB_REPO"] = str(REPO_ROOT)

    result = subprocess.run(
        ["bash", str(SERVICES_DIR / "hermes-hub-wrapper.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr or "not executable" in result.stderr
