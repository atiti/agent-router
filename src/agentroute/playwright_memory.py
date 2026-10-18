"""Install an explicit bounded-history launcher around an existing Playwright CLI."""

import json
import os
import shlex
import shutil
from pathlib import Path

from .config import agentroute_home


def install_guard(original: Path | None = None) -> dict[str, str]:
    home = agentroute_home()
    target = home / "bin" / "playwright-cli"
    state = home / "state" / "playwright-memory.json"
    if original is None:
        found = shutil.which("playwright-cli")
        if found is None:
            raise ValueError("playwright-cli is not installed")
        original = Path(found)
        if original.absolute() == target.absolute():
            original = Path(json.loads(state.read_text())["original"])
    original = original.resolve(strict=True)
    if original == target.resolve():
        raise ValueError("original Playwright launcher resolves to the guard itself")
    guard = home / "bin" / "playwright-memory-guard.cjs"
    guard.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name("playwright_memory_guard.cjs"), guard)
    node_require = f'--require="{guard}"'
    text = (
        "#!/bin/sh\nset -eu\n"
        f'NODE_OPTIONS="${{NODE_OPTIONS:+$NODE_OPTIONS }}"{shlex.quote(node_require)}\n'
        "export NODE_OPTIONS\n"
        f'exec {shlex.quote(str(original))} "$@"\n'
    )
    temporary = target.with_suffix(".new")
    temporary.write_text(text)
    temporary.chmod(0o755)
    os.replace(temporary, target)
    state.parent.mkdir(parents=True, exist_ok=True)
    receipt = {"original": str(original), "launcher": str(target), "guard": str(guard)}
    state.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt
