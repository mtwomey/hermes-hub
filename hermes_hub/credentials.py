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


def _read_keychain_account(account: str) -> str:
    """Read one Keychain account without exposing its value outside process memory."""
    security_path = shutil.which("security")
    if not security_path:
        return ""
    try:
        proc = subprocess.run(
            [security_path, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return ""
    return (proc.stdout or "").rstrip("\n") if proc.returncode == 0 else ""


def require_hub_credentials() -> tuple[str, str]:
    """Return managed hub external/spoke tokens from Keychain or fail closed."""
    external = _read_keychain_account("hub:external:token")
    spoke = _read_keychain_account("hub:spoke:token")
    if not external or not spoke:
        raise CredentialUnavailable("managed hub requires Keychain hub credentials")
    return external, spoke


def require_join_credential() -> str:
    """Resolve the shared spoke-registration ("join") secret (M1, W4b).

    Answers only V5's question (a) -- "may this spoke join the hub at
    all" -- and is deliberately decoupled from any spoke's per-command
    V5a credential (``spoke:<name>:credential``). Reads the SAME Keychain
    account the hub already expects at registration
    (``hub:spoke:token``, see :func:`require_hub_credentials`), so a
    managed spoke's registration token and the hub's expected token stay
    one shared secret -- while each spoke's command-authorization
    credential remains independently distinct.

    Fails closed like :func:`require_spoke_credential`: no environment
    override (launchd plist EnvironmentVariables are world-readable),
    Keychain only.
    """
    value = _read_keychain_account("hub:spoke:token")
    if not value:
        raise CredentialUnavailable(
            f"managed spoke requires the Keychain join credential {KEYCHAIN_SERVICE!r} / "
            "'hub:spoke:token'"
        )
    return value


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


class CredentialTopologyError(RuntimeError):
    """Raised by :func:`assert_distinct_credential_topology` when unrelated
    credential groups share a value, or an intended-matching group's members
    disagree (M1, Gate 1 — the assertion whose absence let every account on
    Pumpkin and Olive collapse to one shared secret)."""


def assert_distinct_credential_topology(
    values: "dict[str, str]", groups: "dict[str, list[str]]"
) -> None:
    """Verify per-peer credential distinctness.

    ``values`` maps an account label (any string identifying one Keychain
    read, e.g. ``"spoke:Olive:credential"`` or ``"caller:Pumpkin:credential
    (Olive)"`` when the same account name is read from two machines) to its
    resolved secret. ``groups`` maps a group name to the list of account
    labels that are supposed to hold ONE shared value (a caller/spoke PAIR,
    or a token that must match across machines).

    Raises :class:`CredentialTopologyError` if:

    * any account in a group has an empty/missing value (never treated as
      "matching" -- see the plan's ``e3b0c44298fc1c14`` note for why),
    * a group's accounts do not all resolve to the same value, or
    * two DIFFERENT groups resolve to the same value (unrelated accounts
      sharing a secret -- the exact defect this function exists to catch).
    """
    group_values: "dict[str, str]" = {}
    for group_name, accounts in groups.items():
        resolved = {values.get(a, "") for a in accounts}
        if "" in resolved:
            missing = [a for a in accounts if not values.get(a, "")]
            raise CredentialTopologyError(
                f"group {group_name!r} has missing/empty value(s) for: {missing}"
            )
        if len(resolved) != 1:
            raise CredentialTopologyError(
                f"group {group_name!r} accounts do not all match: {accounts}"
            )
        group_values[group_name] = resolved.pop()

    seen: "dict[str, str]" = {}
    for group_name, value in group_values.items():
        for other_name, other_value in seen.items():
            if value == other_value:
                raise CredentialTopologyError(
                    f"unrelated groups {group_name!r} and {other_name!r} share "
                    "the same credential value"
                )
        seen[group_name] = value


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
