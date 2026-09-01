"""Portable spoke credential resolution (Task 1.5, V5/V5a).

A spoke needs its own local secret to compare inbound task credentials
against (V5: the spoke enforces, the hub only relays). Resolution order,
first hit wins:

1. explicit constructor/call argument (tests, and the future RPi hub)
2. env var ``HERMES_HUB_SPOKE_CREDENTIAL``
3. macOS Keychain: service ``hermes-hub``, account ``spoke:<name>:credential``
4. unset -> dev mode (empty string; caller treats this as "allow", mirroring
   hermes-peer's D5 / hub_server._check_token behavior)

Keychain access is optional and lazily invoked via ``subprocess`` so this
module imports cleanly on Linux (V8 portability) even when the ``security``
CLI does not exist. A missing binary, or a miss in the keychain, is not an
error -- both fall through to dev mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess

KEYCHAIN_SERVICE = "hermes-hub"
ENV_VAR_NAME = "HERMES_HUB_SPOKE_CREDENTIAL"


class CredentialUnavailable(RuntimeError):
    """The managed-service Keychain credential required by V5a is absent or
    an unsafe environment override was supplied."""


def _keychain_account(spoke_name: str) -> str:
    return f"spoke:{spoke_name}:credential"


def _read_keychain(spoke_name: str) -> str:
    """Best-effort macOS Keychain read. Returns "" on any failure, missing
    binary, or non-macOS host -- never raises."""
    security_path = shutil.which("security")
    if not security_path:
        return ""
    try:
        proc = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                _keychain_account(spoke_name),
                "-w",
            ],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").rstrip("\n")


def require_spoke_credential(spoke_name: str) -> str:
    """Resolve a V5a credential for a *managed* spoke, failing closed.

    Unlike :func:`resolve_spoke_credential`, this deliberately rejects the
    environment variable: launchd plist EnvironmentVariables are
    world-readable. Managed services must obtain the value only from the
    macOS Keychain account ``spoke:<name>:credential``. The returned value
    stays in process memory and must never be logged or placed in argv.
    """
    if os.environ.get(ENV_VAR_NAME, ""):
        raise CredentialUnavailable(
            "managed spoke refuses a credential from environment; use the Keychain"
        )
    keychain_value = _read_keychain(spoke_name)
    if not keychain_value:
        raise CredentialUnavailable(
            f"managed spoke requires Keychain credential {KEYCHAIN_SERVICE!r} / "
            f"{_keychain_account(spoke_name)!r}"
        )
    return keychain_value


def resolve_spoke_credential(spoke_name: str, *, explicit: str = "") -> str:
    """Resolve the credential a spoke named ``spoke_name`` should compare
    inbound task credentials against.

    Returns "" when nothing is configured (dev mode: allow).
    """
    if explicit:
        return explicit

    env_value = os.environ.get(ENV_VAR_NAME, "")
    if env_value:
        return env_value

    keychain_value = _read_keychain(spoke_name)
    if keychain_value:
        return keychain_value

    return ""
