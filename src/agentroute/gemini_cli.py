"""Gemini API backend: OAuth login, bridge lifecycle, and Codex registration."""

from __future__ import annotations

import base64
import datetime
import hashlib
import http.server
import json
import os
import platform
import plistlib
import secrets
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import typer

from .bridge_service import _agentroute_executable, wait_for_health
from .capacity import backend_state
from .config import (
    ExecutionBackendConfig,
    ModelCapabilities,
    ModelTarget,
    load_config,
    save_config,
)
from .gemini_bridge import (
    AUTHORIZE_URL,
    CONTEXT_WINDOW,
    GENERATIVE_SCOPE,
    TOKEN_URL,
    CredentialError,
    choose_models,
    credential_for_transport,
    credential_path,
    environment,
    list_models,
    model_map,
    price_family,
    probe,
    save_model_map,
    serve,
    transport,
    vertex_project,
)
from .pricing import canonical_model, token_cost_upper_bound, turn_cost
from .providers import sync_codex_providers

DEFAULT_PORT = 8817
LOGIN_PORT = 8818
LABEL = "com.agentroute.gemini-bridge"
UNIT = "agentroute-gemini-bridge.service"
TIER_EFFORT = {"fast": "low", "normal": "medium", "smart": "high", "max": "xhigh"}

app = typer.Typer(no_args_is_help=True, help="Route Codex through Google Gemini with OAuth.")

_CALLBACK: dict[str, str] = {}
_CALLBACK_EVENT = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if "code" in params:
            _CALLBACK["state"] = params.get("state", [""])[0]
            _CALLBACK["code"] = params["code"][0]
            body = b"AgentRoute: sign-in complete. You can close this tab."
        elif "error" in params:
            _CALLBACK["error"] = params["error"][0]
            body = b"AgentRoute: sign-in was not completed."
        else:
            body = b"AgentRoute: waiting for Google's response."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _CALLBACK_EVENT.set()


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def unit_path() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    return Path.home() / ".config/systemd/user" / UNIT


def ensure_backend(config, port: int = DEFAULT_PORT, models: dict[str, str] | None = None) -> bool:
    """Register the Gemini backend without changing default tier routing."""
    models = models or model_map()
    created = "gemini" not in config.backends
    tiers = {
        tier: ModelTarget(
            model=model,
            reasoning_effort=TIER_EFFORT[tier],
            context_window=CONTEXT_WINDOW,
        )
        for tier, model in models.items()
        if tier in TIER_EFFORT
    }
    if created:
        config.backends["gemini"] = ExecutionBackendConfig(
            enabled=True,
            codex_provider="agentroute-gemini",
            display_name="Google Gemini (OAuth)",
            base_url=f"http://127.0.0.1:{port}/v1",
            tool_compatibility="functions_and_apply_patch_preserve_reasoning",
            review_model=models.get("fast") or models["normal"],
            tiers=tiers,
        )
    else:
        backend = config.backends["gemini"]
        backend.enabled = True
        backend.base_url = f"http://127.0.0.1:{port}/v1"
        backend.tiers = tiers
    for model in models.values():
        declared = config.capabilities.models.get(model)
        family = price_family(model)
        update = {
            "tool_calling": "full",
            "reasoning": True,
            "vision": True,
            "context_window": CONTEXT_WINDOW,
            "pricing_model": family,
        }
        config.capabilities.models[model] = (
            declared.model_copy(update=update) if declared else ModelCapabilities(**update)
        )
        # Audit rows carry the model ID, so the family is what makes captured
        # Gemini usage price the same way every other backend does. It is not a
        # catalog alias: those must resolve to a real catalog model.
        config.pricing.families[model] = family
        config.pricing.aliases.pop(model, None)
    return created


def discover() -> tuple[dict[str, str], list[str]]:
    """Ask Google which chat models this credential may use, then map tiers."""
    token = credential_for_transport().token()
    available = list_models(token)
    models = choose_models(available)
    save_model_map(models, [name.rsplit("/", 1)[-1] for name in available])
    return models, available


def _exchange(code: str, verifier: str, client_id: str, client_secret: str, redirect: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "user-agent": "agentroute-gemini",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise typer.BadParameter(
            f"Google rejected the authorization code ({exc.code}): {detail[:200]}"
        ) from exc


def client_from_secrets_file(path: Path) -> tuple[str, str]:
    """Read the JSON Google gives you when a Desktop client is downloaded."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise typer.BadParameter(f"no such client secrets file: {path}") from exc
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"cannot read {path}: {exc}") from exc
    body = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        raise typer.BadParameter(
            f"{path} is not a Desktop OAuth client file (expected an 'installed' object)"
        )
    client_id = body.get("client_id")
    client_secret = body.get("client_secret")
    if not client_id or not client_secret:
        raise typer.BadParameter(f"{path} is missing client_id or client_secret")
    return str(client_id), str(client_secret)


def default_client_secrets_path() -> Path:
    override = os.environ.get("AGENTROUTE_GEMINI_CLIENT_SECRETS")
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("AGENTROUTE_HOME") or (Path.home() / ".agentroute"))
    return home / "gemini-oauth-client.json"


@app.command("login")
def login_command(
    client_id: str = typer.Option(None, "--client-id", envvar="AGENTROUTE_GEMINI_CLIENT_ID"),
    client_secret: str = typer.Option(
        None, "--client-secret", envvar="AGENTROUTE_GEMINI_CLIENT_SECRET"
    ),
    secrets_file: Path = typer.Option(
        None,
        "--client-secrets",
        help="Downloaded Desktop client JSON. Default: ~/.agentroute/gemini-oauth-client.json",
    ),
    port: int = typer.Option(LOGIN_PORT, help="Loopback port for the OAuth callback."),
) -> None:
    """Authorize Gemini API access with Google OAuth and store the refresh token."""
    if not (client_id and client_secret):
        candidate = (
            Path(secrets_file).expanduser() if secrets_file else default_client_secrets_path()
        )
        if candidate.exists():
            client_id, client_secret = client_from_secrets_file(candidate)
            typer.echo(f"using the OAuth client in {candidate}")
    if not client_id or not client_secret:
        typer.echo(
            "A Desktop OAuth client is required once.\n"
            "  1. Open https://console.cloud.google.com/apis/library/generativelanguage.googleapis.com\n"
            "     and enable the Generative Language API.\n"
            "  2. Open https://console.cloud.google.com/apis/credentials and create an\n"
            "     OAuth client of type 'Desktop app'.\n"
            "  3. Download its JSON to ~/.agentroute/gemini-oauth-client.json, then\n"
            "     rerun: agentroute gemini login\n"
            "     (or pass --client-id ID --client-secret SECRET)\n"
            "These values identify your project. Gemini OAuth usage is billed there."
        )
        raise typer.Exit(2)
    _CALLBACK.clear()
    _CALLBACK_EVENT.clear()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)
    redirect = f"http://127.0.0.1:{port}/oauth2callback"
    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as exc:
        typer.echo(f"cannot listen on 127.0.0.1:{port} for the callback: {exc}")
        raise typer.Exit(1) from exc
    threading.Thread(target=server.handle_request, daemon=True).start()
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": GENERATIVE_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    typer.echo("Open this URL and approve Gemini access:\n")
    typer.echo(f"{AUTHORIZE_URL}?{query}\n")
    try:
        if not _CALLBACK_EVENT.wait(timeout=600):
            typer.echo("Timed out waiting for the Google sign-in.")
            raise typer.Exit(1)
    finally:
        server.server_close()
    if "code" not in _CALLBACK:
        typer.echo(f"Sign-in failed: {_CALLBACK.get('error', 'no authorization code')}")
        raise typer.Exit(1)
    if _CALLBACK.get("state") != state:
        typer.echo("Sign-in failed: the callback state did not match this request.")
        raise typer.Exit(1)
    payload = _exchange(_CALLBACK["code"], verifier, client_id, client_secret, redirect)
    if not payload.get("refresh_token"):
        typer.echo("Google returned no refresh token; rerun with a fresh consent.")
        raise typer.Exit(1)
    path = credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": payload["refresh_token"],
                "scope": payload.get("scope") or GENERATIVE_SCOPE,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    typer.echo(f"stored the Gemini OAuth credential in {path}")


@app.command("check")
def check_command(
    inference: bool = typer.Option(
        True, "--inference/--no-inference", help="Send one minimal prompt to prove inference."
    ),
) -> None:
    """Verify the credential, resolve models, and optionally run one live turn."""
    try:
        credential_for_transport().check()
    except CredentialError as exc:
        typer.echo(f"Gemini credential unavailable: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"transport: {transport()}")
    if transport() == "vertex":
        typer.echo(f"project:   {vertex_project()}")
    else:
        typer.echo(f"credential: {credential_path()}")
    models = model_map()
    for tier, model in models.items():
        typer.echo(f"{tier:6} {model}  ({price_family(model)} rates)")
    try:
        from .audit import AuditStore

        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)
        totals = usage_totals(AuditStore().rows_since(since), load_config())
        today = totals["today"]
        typer.echo(
            f"usage: today {today['turns']} turns · ${today['cost_usd']:.2f} · "
            f"{_tokens(today['input_tokens'])} in / {_tokens(today['output_tokens'])} out"
        )
    except Exception as exc:  # audit store is optional for a bare credential check
        typer.echo(f"usage: unavailable ({type(exc).__name__})")
    if inference:
        model = models["normal"]
        try:
            text = probe(model)
        except CredentialError as exc:
            typer.echo(f"inference failed: {exc}")
            raise typer.Exit(1) from exc
        typer.echo(f"inference: {model} replied {text.strip()[:60]!r}")


@app.command("models")
def models_command(
    refresh: bool = typer.Option(False, "--refresh", help="Query Google again."),
) -> None:
    """List available Gemini chat models and the tier mapping."""
    available: list[str] = []
    if refresh:
        try:
            models, available = discover()
        except CredentialError as exc:
            typer.echo(f"cannot discover models: {exc}")
            raise typer.Exit(1) from exc
    else:
        models = model_map()
    for tier, model in models.items():
        typer.echo(f"{tier:6} {model}")
    if available:
        typer.echo("\navailable:")
        for name in sorted({entry.rsplit("/", 1)[-1] for entry in available}):
            typer.echo(f"  {name}")


def usage_totals(rows, config, *, now: datetime.datetime | None = None) -> dict[str, object]:
    """Summarize recorded Gemini turns for today and this month.

    Token counters come from Codex's usage receipts through the audit store, so
    this reports the same measured numbers as every other backend rather than a
    provider estimate.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = {
        "turns": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    month = dict(today)
    models: dict[str, dict[str, float]] = {}
    unpriced: set[str] = set()
    for row in rows:
        if row["usage_recorded_at"] is None:
            continue
        if str(row["backend"] or "") != "gemini":
            continue
        try:
            created = datetime.datetime.fromisoformat(str(row["created_at"])).astimezone(
                datetime.timezone.utc
            )
        except ValueError:
            continue
        if (created.year, created.month) != (now.year, now.month):
            continue
        model = str(row["answer_model"] or row["model"] or "unknown")
        usage = {
            "input_tokens": int(row["answer_input_tokens"] or 0),
            "cached_input_tokens": int(row["answer_cached_input_tokens"] or 0),
            "cache_write_input_tokens": int(row["answer_cache_write_input_tokens"] or 0),
            "output_tokens": int(row["answer_output_tokens"] or 0),
        }
        cost = turn_cost(row, model, config.pricing)
        if cost is None:
            cost = token_cost_upper_bound(model, usage, config.pricing)
        if canonical_model(model, config.pricing) not in config.pricing.models:
            unpriced.add(model)
        for bucket in (month, today if created.date() == now.date() else None):
            if bucket is None:
                continue
            bucket["turns"] += 1
            bucket["input_tokens"] += usage["input_tokens"]
            bucket["cached_input_tokens"] += usage["cached_input_tokens"]
            bucket["output_tokens"] += usage["output_tokens"]
            bucket["cost_usd"] += cost
        entry = models.setdefault(model, {"turns": 0, "cost_usd": 0.0, "output_tokens": 0})
        entry["turns"] += 1
        entry["cost_usd"] += cost
        entry["output_tokens"] += usage["output_tokens"]
    return {
        "today": today,
        "month": month,
        "models": models,
        "unpriced": sorted(unpriced),
    }


def _tokens(value: int) -> str:
    return f"{value / 1_000_000:.2f}M" if value >= 1_000_000 else f"{value:,}"


@app.command("usage")
def usage_command(
    days: int = typer.Option(31, help="Window to read from the audit store."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show measured Gemini tokens, spend, and budget headroom."""
    from .audit import AuditStore

    config = load_config()
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    totals = usage_totals(AuditStore().rows_since(since), config)
    backend = config.backends.get("gemini")
    daily_budget = backend.daily_budget_usd if backend else None
    monthly_budget = backend.monthly_budget_usd if backend else None
    today, month = totals["today"], totals["month"]
    report = {
        "transport": transport(),
        "today": today,
        "month": month,
        "daily_budget_usd": daily_budget,
        "monthly_budget_usd": monthly_budget,
        "daily_used_percent": (
            round(today["cost_usd"] / daily_budget * 100, 1) if daily_budget else None
        ),
        "monthly_used_percent": (
            round(month["cost_usd"] / monthly_budget * 100, 1) if monthly_budget else None
        ),
        "models": totals["models"],
        "unpriced_models": totals["unpriced"],
    }
    if json_output:
        typer.echo(json.dumps(report, indent=2))
        return
    typer.echo(f"Gemini usage ({transport()} transport, measured from audit receipts)")
    for label, bucket in (("today", today), ("month", month)):
        typer.echo(
            f"  {label:5} {bucket['turns']:>4} turns · "
            f"{_tokens(bucket['input_tokens'])} in "
            f"({_tokens(bucket['cached_input_tokens'])} cached) · "
            f"{_tokens(bucket['output_tokens'])} out · ${bucket['cost_usd']:.2f}"
        )
    if daily_budget or monthly_budget:
        daily = f"${daily_budget:g}" if daily_budget else "unlimited"
        monthly = f"${monthly_budget:g}" if monthly_budget else "unlimited"
        typer.echo(f"  budget daily {daily} · monthly {monthly}")
        state = backend_state(
            config,
            "gemini",
            daily_spend=today["cost_usd"],
            monthly_spend=month["cost_usd"],
            probe=False,
        )
        typer.echo(f"  status {state.status}: {state.detail}")
    else:
        typer.echo(
            "  budget unlimited; set one with "
            "`agentroute capacity budget gemini --daily 10 --monthly 100`"
        )
    if totals["models"]:
        typer.echo("  by model")
        for model, entry in sorted(totals["models"].items(), key=lambda item: -item[1]["cost_usd"]):
            typer.echo(
                f"    {model:28} {int(entry['turns']):>4} turns · "
                f"{_tokens(int(entry['output_tokens']))} out · ${entry['cost_usd']:.2f}"
            )
    if totals["unpriced"]:
        typer.echo(
            "  unpriced: "
            + ", ".join(totals["unpriced"])
            + " (add a rate in ~/.agentroute/config.yaml under pricing.models)"
        )
    typer.echo(
        "  Google does not publish Gemini API quota; spend budgets above are the "
        "local guard, and a 429 surfaces Google's own retry window."
    )


@app.command("serve")
def serve_command(
    port: int = typer.Option(DEFAULT_PORT, help="Loopback port to bind."),
    host: str = typer.Option("127.0.0.1", help="Loopback host to bind."),
) -> None:
    """Run the Gemini bridge in the foreground."""
    serve(host=host, port=port)


def service_definition(port: int) -> dict:
    log_path = Path.home() / ".agentroute" / "gemini-bridge.log"
    values = environment()
    definition = {
        "Label": LABEL,
        "ProgramArguments": [
            str(_agentroute_executable()),
            "gemini",
            "serve",
            "--port",
            str(port),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if values:
        # launchd does not inherit the shell environment that chose the transport.
        definition["EnvironmentVariables"] = values
    return definition


def systemd_unit_text(port: int) -> str:
    lines = [
        "[Unit]",
        "Description=AgentRoute Gemini bridge (Google OAuth to Responses)",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
    ]
    lines.extend(f"Environment={key}={value}" for key, value in environment().items())
    lines.extend(
        [
            f"ExecStart={_agentroute_executable()} gemini serve --port {port}",
            "Restart=always",
            "RestartSec=3",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return "\n".join(lines)


def _run(*command: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=check)


def load_service(path: Path) -> None:
    if platform.system() == "Darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        _run("launchctl", "bootstrap", f"gui/{os.getuid()}", str(path), check=True)
        return
    _run("systemctl", "--user", "daemon-reload", check=True)
    _run("systemctl", "--user", "enable", "--now", UNIT, check=True)


def unload_service() -> None:
    if platform.system() == "Darwin":
        _run("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
        return
    _run("systemctl", "--user", "disable", "--now", UNIT)


@app.command("install")
def install_command(
    port: int = typer.Option(DEFAULT_PORT, help="Loopback port to bind."),
    start: bool = typer.Option(
        True, "--service/--no-service", help="Start the background service."
    ),
) -> None:
    """Register the backend, resolve real model IDs, and start the bridge."""
    available: list[str] = []
    verified = True
    try:
        credential_for_transport().check()
        models, available = discover()
    except CredentialError as exc:
        # Install stays useful before the one-time Google consent: the service
        # starts and reports the credential problem instead of leaving nothing
        # behind for `agentroute gemini check` to talk to.
        verified = False
        models = model_map()
        typer.echo(f"Gemini credential not usable yet: {exc}")
        typer.echo("Registering the backend with unverified model IDs instead.")
    config = load_config()
    ensure_backend(config, port, models)
    save_config(config)
    sync_codex_providers(config)
    typer.echo(f"registered backend `gemini` at http://127.0.0.1:{port}/v1")
    for tier, model in models.items():
        typer.echo(f"  {tier:6} {model}")
    if verified:
        typer.echo(f"discovered {len({name.rsplit('/', 1)[-1] for name in available})} chat models")
    else:
        typer.echo("run `agentroute gemini login` then `agentroute gemini install` to verify")
    if not start:
        return
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        path.write_bytes(plistlib.dumps(service_definition(port)))
    else:
        path.write_text(systemd_unit_text(port))
    load_service(path)
    health = wait_for_health(port, host="127.0.0.1", timeout=20)
    if health.status == "unreachable":
        typer.echo(f"installed at {path}, but the bridge did not answer: {health.detail}")
        raise typer.Exit(1)
    typer.echo(f"bridge healthy at http://127.0.0.1:{port} ({health.detail})")


@app.command("uninstall")
def uninstall_command() -> None:
    """Stop the bridge service and remove its unit."""
    unload_service()
    path = unit_path()
    if path.exists():
        path.unlink()
    typer.echo(f"stopped Gemini bridge ({path})")
