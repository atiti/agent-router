"""Private alias bindings from task names to existing Chromium browser profiles.

AgentRoute does not drive the browser itself. The bundled ChatGPT browser plugin
already selects a profile through ``CODEX_CHROMIUM_USER_DATA_DIR`` and
``CODEX_CHROMIUM_PREFERENCES_PATH``; this module only records which existing
profile directory an alias means, validates it, and produces those overrides.

Bindings live in a private sidecar (``browser-profiles.json`` under the
AgentRoute home, mode 0600 in a 0700 directory). Nothing here signs in,
copies cookies, or infers which account a profile belongs to.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import agentroute_home, codex_home

SCHEMA_VERSION = 1
SIDECAR_NAME = "browser-profiles.json"
MODES = ("off", "browser", "computer", "both")
DEFAULT_MODE = "browser"
USER_DATA_ENV = "CODEX_CHROMIUM_USER_DATA_DIR"
PREFERENCES_ENV = "CODEX_CHROMIUM_PREFERENCES_PATH"
PLUGIN_DIR_ENV = "AGENTROUTE_BROWSER_PLUGIN_DIR"

# Same profile directories the vendor plugin treats as usable.
_PROFILE_DIR = re.compile(r"^(Default|Profile [1-9][0-9]{0,5})$")
_ALIAS = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_TOP_KEYS = {"version", "bindings"}
_BINDING_KEYS = {"profile_dir", "user_data_dir", "mode", "sites", "workflows"}
_MAX_SIDECAR_BYTES = 1_000_000


class BrowserProfileError(ValueError):
    """A binding, profile or sidecar problem the operator must fix."""


@dataclass(frozen=True)
class ChromeProfile:
    profile_dir: str
    label: str
    email: str | None = None


@dataclass(frozen=True)
class Binding:
    alias: str
    profile_dir: str
    mode: str = DEFAULT_MODE
    user_data_dir: str | None = None
    sites: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"profile_dir": self.profile_dir, "mode": self.mode}
        if self.user_data_dir:
            data["user_data_dir"] = self.user_data_dir
        if self.sites:
            data["sites"] = list(self.sites)
        if self.workflows:
            data["workflows"] = list(self.workflows)
        return data


@dataclass(frozen=True)
class ResolvedBinding:
    binding: Binding
    user_data_dir: Path
    profile_path: Path
    preferences_path: Path
    env: dict[str, str] = field(default_factory=dict)

    @property
    def alias(self) -> str:
        return self.binding.alias

    @property
    def mode(self) -> str:
        return self.binding.mode

    @property
    def browser_allowed(self) -> bool:
        return self.binding.mode in ("browser", "both")

    @property
    def computer_allowed(self) -> bool:
        return self.binding.mode in ("computer", "both")

    def to_json(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "mode": self.mode,
            "browser_allowed": self.browser_allowed,
            "computer_allowed": self.computer_allowed,
            "profile_dir": self.binding.profile_dir,
            "user_data_dir": str(self.user_data_dir),
            "preferences_path": str(self.preferences_path),
            "launch_args": [f"--profile-directory={self.binding.profile_dir}"],
            "env": dict(self.env),
            "sites": list(self.binding.sites),
            "workflows": list(self.binding.workflows),
            "authentication": "not checked",
        }


def sidecar_path() -> Path:
    return agentroute_home() / SIDECAR_NAME


def default_user_data_dir() -> Path:
    override = os.environ.get(USER_DATA_ENV)
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(local) / "Google" / "Chrome" / "User Data"
    return home / ".config" / "google-chrome"


def validate_alias(alias: str) -> str:
    if not _ALIAS.fullmatch(alias or ""):
        raise BrowserProfileError(
            f"invalid alias {alias!r}: use 1-63 lowercase letters, digits, '-' or '_'"
        )
    return alias


def validate_profile_dir(name: str) -> str:
    if not _PROFILE_DIR.fullmatch(name or ""):
        raise BrowserProfileError(
            f"invalid profile directory {name!r}: expected 'Default' or 'Profile N'"
        )
    return name


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise BrowserProfileError(f"invalid mode {mode!r}: expected one of {', '.join(MODES)}")
    return mode


def _validate_labels(values: Any, kind: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise BrowserProfileError(f"{kind} must be a list")
    labels: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _LABEL.fullmatch(value):
            raise BrowserProfileError(f"invalid {kind[:-1]} label {value!r}")
        if value not in labels:
            labels.append(value)
    return tuple(labels)


def _user_data_dir(binding: Binding) -> Path:
    if binding.user_data_dir:
        return Path(binding.user_data_dir)
    return default_user_data_dir()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def list_profiles(
    user_data_dir: Path | None = None, include_email: bool = False
) -> list[ChromeProfile]:
    """Read profile labels from Chrome's Local State for directories that exist."""
    root = user_data_dir or default_user_data_dir()
    local_state = root / "Local State"
    try:
        data = _read_json(local_state)
    except FileNotFoundError as exc:
        raise BrowserProfileError(f"no browser Local State found in {root}") from exc
    except (OSError, ValueError) as exc:
        raise BrowserProfileError(f"could not read {local_state}: {exc}") from exc
    cache = (data.get("profile") or {}).get("info_cache") if isinstance(data, dict) else None
    if not isinstance(cache, dict):
        cache = {}
    profiles = []
    for name, info in cache.items():
        if not _PROFILE_DIR.fullmatch(name) or not (root / name / "Preferences").is_file():
            continue
        info = info if isinstance(info, dict) else {}
        label = info.get("name") if isinstance(info.get("name"), str) else ""
        email = info.get("user_name") if include_email else None
        profiles.append(
            ChromeProfile(name, label, email if isinstance(email, str) and email else None)
        )
    return sorted(profiles, key=_profile_sort_key)


def _profile_sort_key(profile: ChromeProfile) -> tuple[int, int]:
    if profile.profile_dir == "Default":
        return (0, 0)
    return (1, int(profile.profile_dir.split()[1]))


def _parse(data: Any) -> dict[str, Binding]:
    if not isinstance(data, dict) or set(data) - _TOP_KEYS:
        raise BrowserProfileError("browser profile sidecar has unknown top-level fields")
    if data.get("version") != SCHEMA_VERSION:
        raise BrowserProfileError(
            f"unsupported browser profile sidecar version {data.get('version')!r}"
        )
    raw = data.get("bindings", {})
    if not isinstance(raw, dict):
        raise BrowserProfileError("browser profile sidecar bindings must be an object")
    bindings: dict[str, Binding] = {}
    for alias, entry in raw.items():
        validate_alias(alias)
        if not isinstance(entry, dict):
            raise BrowserProfileError(f"binding {alias!r} must be an object")
        unknown = set(entry) - _BINDING_KEYS
        if unknown:
            raise BrowserProfileError(
                f"binding {alias!r} has unknown fields: {', '.join(sorted(unknown))}"
            )
        user_data = entry.get("user_data_dir")
        if user_data is not None and (
            not isinstance(user_data, str) or not Path(user_data).is_absolute()
        ):
            raise BrowserProfileError(f"binding {alias!r} user_data_dir must be absolute")
        bindings[alias] = Binding(
            alias=alias,
            profile_dir=validate_profile_dir(entry.get("profile_dir", "")),
            mode=validate_mode(entry.get("mode", DEFAULT_MODE)),
            user_data_dir=user_data,
            sites=_validate_labels(entry.get("sites", []), "sites"),
            workflows=_validate_labels(entry.get("workflows", []), "workflows"),
        )
    return bindings


def load_bindings(path: Path | None = None) -> dict[str, Binding]:
    path = path or sidecar_path()
    if path.is_symlink():
        raise BrowserProfileError(f"{path} must not be a symlink")
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BrowserProfileError(f"{path} must be owned by you with mode 0600")
    if info.st_size > _MAX_SIDECAR_BYTES:
        raise BrowserProfileError(f"{path} is too large")
    try:
        data = _read_json(path)
    except (OSError, ValueError) as exc:
        raise BrowserProfileError(f"could not read {path}: {exc}") from exc
    return _parse(data)


def _private_parent(path: Path) -> None:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or parent.stat().st_uid != os.getuid():
        raise BrowserProfileError(f"{parent} must be a directory owned by you")
    if parent.stat().st_mode & 0o077:
        parent.chmod(0o700)


def _write(path: Path, bindings: dict[str, Binding]) -> None:
    payload = {
        "version": SCHEMA_VERSION,
        "bindings": {alias: bindings[alias].to_json() for alias in sorted(bindings)},
    }
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
    except OSError as exc:
        raise BrowserProfileError(f"could not save {path}: {exc}") from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    _private_parent(path)
    lock = os.open(path.parent / f".{path.name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock)


def _update(
    mutate: Callable[[dict[str, Binding]], Binding | None], path: Path | None = None
) -> Binding | None:
    path = path or sidecar_path()
    with _locked(path):
        if path.is_symlink():
            raise BrowserProfileError(f"{path} must not be a symlink")
        bindings = load_bindings(path)
        result = mutate(bindings)
        _write(path, bindings)
        return result


def _require_existing_profile(user_data_dir: Path, profile_dir: str) -> Path:
    profile_path = user_data_dir / validate_profile_dir(profile_dir)
    if profile_path.is_symlink() or not (profile_path / "Preferences").is_file():
        raise BrowserProfileError(
            f"browser profile {profile_dir!r} does not exist in {user_data_dir}"
        )
    return profile_path


def bind(
    alias: str,
    profile_dir: str,
    *,
    user_data_dir: Path | None = None,
    sites: list[str] | tuple[str, ...] = (),
    workflows: list[str] | tuple[str, ...] = (),
    mode: str | None = None,
    path: Path | None = None,
) -> Binding:
    """Create or replace one alias; an existing mode is kept unless one is given."""
    validate_alias(alias)
    if user_data_dir is not None and not user_data_dir.is_absolute():
        raise BrowserProfileError("--user-data-dir must be an absolute path")
    root = user_data_dir or default_user_data_dir()
    _require_existing_profile(root, profile_dir)
    site_labels = _validate_labels(list(sites), "sites")
    workflow_labels = _validate_labels(list(workflows), "workflows")

    def mutate(bindings: dict[str, Binding]) -> Binding:
        previous = bindings.get(alias)
        chosen = validate_mode(mode) if mode else previous.mode if previous else DEFAULT_MODE
        binding = Binding(
            alias=alias,
            profile_dir=profile_dir,
            mode=chosen,
            user_data_dir=str(user_data_dir) if user_data_dir else None,
            sites=site_labels,
            workflows=workflow_labels,
        )
        bindings[alias] = binding
        return binding

    result = _update(mutate, path)
    assert result is not None
    return result


def unbind(alias: str, path: Path | None = None) -> None:
    def mutate(bindings: dict[str, Binding]) -> None:
        if bindings.pop(validate_alias(alias), None) is None:
            raise BrowserProfileError(f"no browser binding named {alias!r}")

    _update(mutate, path)


def set_mode(alias: str, mode: str, path: Path | None = None) -> Binding:
    validate_mode(mode)

    def mutate(bindings: dict[str, Binding]) -> Binding:
        current = bindings.get(validate_alias(alias))
        if current is None:
            raise BrowserProfileError(f"no browser binding named {alias!r}")
        updated = Binding(
            alias=alias,
            profile_dir=current.profile_dir,
            mode=mode,
            user_data_dir=current.user_data_dir,
            sites=current.sites,
            workflows=current.workflows,
        )
        bindings[alias] = updated
        return updated

    result = _update(mutate, path)
    assert result is not None
    return result


def _select(
    bindings: dict[str, Binding], alias: str | None, site: str | None, workflow: str | None
) -> Binding:
    if alias:
        if site or workflow:
            raise BrowserProfileError("select by alias or by --site/--workflow, not both")
        binding = bindings.get(validate_alias(alias))
        if binding is None:
            raise BrowserProfileError(f"no browser binding named {alias!r}")
        return binding
    if not site and not workflow:
        raise BrowserProfileError("pass an alias, --site or --workflow")
    matches = [
        binding
        for binding in bindings.values()
        if (not site or site in binding.sites) and (not workflow or workflow in binding.workflows)
    ]
    wanted = " and ".join(
        part for part in (site and f"site {site!r}", workflow and f"workflow {workflow!r}") if part
    )
    if not matches:
        raise BrowserProfileError(f"no browser binding is labeled for {wanted}")
    if len(matches) > 1:
        names = ", ".join(sorted(binding.alias for binding in matches))
        raise BrowserProfileError(f"{wanted} is ambiguous between bindings: {names}")
    return matches[0]


def resolve_binding(
    alias: str | None = None,
    *,
    site: str | None = None,
    workflow: str | None = None,
    path: Path | None = None,
) -> ResolvedBinding:
    """Return the exact current profile for one binding, or raise.

    Callers must check ``browser_allowed`` / ``computer_allowed`` before using
    ``env``; resolution itself does not enforce the per-alias mode.
    """
    binding = _select(load_bindings(path), alias, site, workflow)
    root = _user_data_dir(binding)
    profile_path = _require_existing_profile(root, binding.profile_dir)
    preferences = profile_path / "Preferences"
    env = {USER_DATA_ENV: str(root), PREFERENCES_ENV: str(preferences)}
    return ResolvedBinding(binding, root, profile_path, preferences, env)


def plugin_dir() -> Path:
    override = os.environ.get(PLUGIN_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return codex_home() / "plugins" / "cache" / "openai-bundled" / "chrome" / "latest"


Runner = Callable[..., subprocess.CompletedProcess]


def extension_readiness(
    resolved: ResolvedBinding, *, runner: Runner = subprocess.run, timeout: float = 15
) -> dict[str, Any]:
    """Ask the vendor plugin's own checker about the bound profile only."""
    script = plugin_dir() / "scripts" / "check-extension-installed.js"
    if not script.is_file():
        return {"status": "unavailable", "detail": "browser plugin checker not found"}
    environment = {**os.environ, **resolved.env}
    try:
        completed = runner(
            ["node", str(script), "--browser", "chrome", "--json"],
            env=environment, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unavailable", "detail": f"checker did not run: {type(exc).__name__}"}
    try:
        report = json.loads(completed.stdout or "")
    except ValueError:
        return {"status": "unavailable", "detail": f"checker exited {completed.returncode}"}
    if not isinstance(report, dict):
        return {"status": "unavailable", "detail": "checker returned no report"}
    if report.get("selectedProfileDirectory") not in (None, resolved.binding.profile_dir):
        return {"status": "unavailable", "detail": "checker inspected a different profile"}
    if report.get("enabled") is True:
        status = "enabled"
    elif report.get("installed") is True:
        status = "disabled"
    elif report.get("installed") is False:
        status = "not_installed"
    else:
        return {"status": "unavailable", "detail": f"checker exited {completed.returncode}"}
    return {"status": status}
