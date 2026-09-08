"""Strategy-plugin management commands (S3 authoring + install UX).

`jarvis plugin new` scaffolds a ready-to-edit plugin package; `jarvis
plugin install` installs one (a local package dir, editable, or a PyPI
name), adds its declared strategy names to the allow-list, and prints the
restart reminder. Pure sugar over the D35/D36 flow — discovery, gating,
and failure recording still live in the loader; no contract changes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import typer
from rich.console import Console

plugin_app = typer.Typer(no_args_is_help=True, help="Manage strategy plugins")

console = Console()
err_console = Console(stderr=True)

ALLOWLIST_VAR = "JARVIS_STRATEGY_PLUGIN_ALLOWLIST"
_ENTRY_GROUP = "jarvis.strategies"
_BUILTIN_NAMES = ("function_calling", "react")

# Scaffold templates. Tokens (__TOKEN__ style) are replaced verbatim —
# brace-free on purpose, so generated f-strings need no escaping.

_PYPROJECT = """[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "__DIST__"
version = "0.1.0"
description = "__DESC__"
requires-python = ">=3.12"
# NOTE: no `jarvis` dependency — the platform that loads this plugin
# already provides it, and jarvis is not on PyPI (declaring it would make
# every install try to resolve it from PyPI and fail).

[project.entry-points."jarvis.strategies"]
__NAME__ = "__MODULE__.strategy:__CLASS__"

[tool.hatch.build.targets.wheel]
packages = ["src/__MODULE__"]
"""

_INIT = '''"""__DIST__ — a JARVIS strategy plugin (S3)."""

from .strategy import __CLASS__

__all__ = ["__CLASS__"]
'''

_STRATEGY = '''"""__NAME__ strategy — a JARVIS strategy plugin (S3, D35/D36).

The contract (docs/plugins/strategy-plugins.md), in short:

- step() performs AT MOST ONE model invocation and never loops;
- return ToolCallsStep (hand tool calls to the orchestrator, or a think
  step with empty tool_calls — the assistant message persists, the loop
  continues) or FinishStep (end the run);
- terminal events, limits, and cancellation belong to the orchestrator;
- stream via client.stream() and forward text.delta to the sink, so the
  run console renders your replies live (a generate()-only plugin shows
  empty iterations there).

This template is a SINGLE-SHOT strategy: one invocation, then finish.
Replace the instruction with your own recipe — or model richer phases on
tests/fixtures/strategies/jarvis-strategy-fixtures (plan_execute,
tree_of_thoughts): phases advance by transcript position, never marker
detection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from jarvis.domain.events import (
    ModelInvocationCompleted,
    ModelInvocationStarted,
    TextDelta,
)
from jarvis.domain.execution import ExecutionContext
from jarvis.domain.message import Message, Usage
from jarvis.domain.message import developer as developer_message
from jarvis.models.types import (
    FinishDelta,
    ModelRequest,
    ModelResponse,
    UsageDelta,
)
from jarvis.models.types import (
    TextDelta as ModelTextDelta,
)
from jarvis.ports.events import EventSink
from jarvis.ports.model import ModelClient
from jarvis.ports.strategy import FinishStep, StepOutcome


class __CLASS__:
    @property
    def name(self) -> str:
        return "__NAME__"

    async def step(
        self,
        ctx: ExecutionContext,
        messages: list[Message],
        client: ModelClient,
        tools: list[Any],
        sink: EventSink,
    ) -> StepOutcome:
        instruction = "TODO: replace with your own instruction for the model."
        request = ModelRequest(
            model="",
            messages=[*messages, developer_message(instruction)],
            temperature=ctx.temperature,
        )
        response = await _invoke(ctx, request, client, sink)
        return FinishStep(assistant_message=response.message, finish_reason="stop")


async def _invoke(
    ctx: ExecutionContext,
    request: ModelRequest,
    client: ModelClient,
    sink: EventSink,
) -> ModelResponse:
    """One model invocation, streamed — the same discipline the core
    strategies use. Events go through the sink only; usage accumulates on
    the ctx so budget and limits see this call."""
    await sink.append(
        ModelInvocationStarted(
            event_id=str(uuid4()), run_id=ctx.run_id, created_at=datetime.now(UTC)
        )
    )
    text_parts: list[str] = []
    usage = Usage()
    finish_reason = "stop"
    model = request.model
    if client.capabilities.streaming:
        async for delta in client.stream(request, cancel=ctx.cancel):
            # NOTE: the stream delta and the sink event are two different
            # classes, both named TextDelta — isinstance-check the MODEL
            # delta, append the DOMAIN event.
            if isinstance(delta, ModelTextDelta):
                text_parts.append(delta.text)
                await sink.append(
                    TextDelta(
                        event_id=str(uuid4()),
                        run_id=ctx.run_id,
                        created_at=datetime.now(UTC),
                        text=delta.text,
                    )
                )
            elif isinstance(delta, UsageDelta):
                usage = delta.usage
            elif isinstance(delta, FinishDelta):
                finish_reason = delta.finish_reason
                model = delta.model or request.model
    else:
        response = await client.generate(request, cancel=ctx.cancel)
        text_parts.append(response.message.text)
        usage = response.usage
        finish_reason = response.finish_reason
        model = response.model
    ctx.usage = ctx.usage.plus(usage)
    await sink.append(
        ModelInvocationCompleted(
            event_id=str(uuid4()),
            run_id=ctx.run_id,
            created_at=datetime.now(UTC),
            usage=usage,
            finish_reason=finish_reason,
            model=model,
        )
    )
    return ModelResponse(
        message=Message(role="assistant", content="".join(text_parts)),
        usage=usage,
        finish_reason=finish_reason,
        model=model,
    )
'''

_README = """# __DIST__

A JARVIS strategy plugin (S3). Edit `src/__MODULE__/strategy.py` — the
`step()` method is your loop recipe — then install:

```bash
jarvis plugin install ./__NAME__
```

The full third-party contract: `docs/plugins/strategy-plugins.md` in the
JARVIS repository. Working samples: `plan_execute`, `tree_of_thoughts`.
"""

_TEMPLATES: dict[str, str] = {
    "pyproject.toml": _PYPROJECT,
    "src/__MODULE__/__init__.py": _INIT,
    "src/__MODULE__/strategy.py": _STRATEGY,
    "README.md": _README,
}


def _normalize(name: str) -> str:
    normalized = name.strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized.isidentifier():
        err_console.print(
            f"[red]{name!r}[/red] must be letters, digits, underscores or hyphens "
            "(it becomes a Python module and a strategy name)"
        )
        raise typer.Exit(1)
    return normalized


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_")) + "Strategy"


@plugin_app.command("new")
def plugin_new(name: str) -> None:
    """Scaffold a strategy plugin package in ./<name>/ — edit strategy.py,
    then `jarvis plugin install ./<name>`."""
    module = _normalize(name)
    class_name = _class_name(module)
    dest = Path.cwd() / module
    if dest.exists():
        err_console.print(f"[red]{dest} already exists[/red]")
        raise typer.Exit(1)
    tokens = {
        "__NAME__": module,
        "__MODULE__": f"jarvis_{module}",
        "__CLASS__": class_name,
        "__DIST__": f"jarvis-{module.replace('_', '-')}",
        "__DESC__": f"{module} strategy plugin for JARVIS",
    }
    for rel, template in _TEMPLATES.items():
        path = dest / rel.replace("__MODULE__", tokens["__MODULE__"])
        path.parent.mkdir(parents=True, exist_ok=True)
        content = template
        for token, value in tokens.items():
            content = content.replace(token, value)
        path.write_text(content)
    console.print(f"[green]scaffolded[/green] {dest}/")
    shadow = "  [yellow](shadows a builtin name)[/yellow]" if module in _BUILTIN_NAMES else ""
    console.print(f"  strategy name: {module}{shadow}")
    console.print(f"next: edit src/{tokens['__MODULE__']}/strategy.py", style="dim")
    console.print(f"then: [bold]jarvis plugin install ./{module}[/bold]")


@plugin_app.command("install")
def plugin_install(target: str) -> None:
    """Install a strategy plugin (local package dir, editable, or PyPI name)
    and add its strategy names to the allow-list."""
    path = Path(target)
    is_local = path.is_dir()
    dist = _dist_name(path) if is_local else target
    _run_installer(target, editable=is_local)
    names = _entry_point_names(dist)
    if not names:
        err_console.print(
            f"[yellow]installed {dist}, but it declares no [bold]{_ENTRY_GROUP}"
            f"[/bold] entry points — nothing to allow-list[/yellow]"
        )
        raise typer.Exit(1)
    added = _append_allowlist(sorted(names), Path.cwd() / ".env")
    if not added:
        console.print(f"[green]already allow-listed[/green]: {', '.join(sorted(names))}")
    else:
        console.print(f"[green]allow-list updated[/green] — added: {', '.join(added)}")
        if os.environ.get(ALLOWLIST_VAR):
            err_console.print(
                f"[yellow]note: {ALLOWLIST_VAR} is also set in your shell — the "
                "shell value wins over .env; update it there too[/yellow]"
            )
    console.print(
        "restart the server ([bold]uv run jarvis serve[/bold]) and any workers to load the plugin"
    )


def _dist_name(path: Path) -> str:
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        err_console.print(f"[red]{path} has no pyproject.toml — not a plugin package[/red]")
        raise typer.Exit(1)
    name = tomllib.loads(pyproject.read_text()).get("project", {}).get("name")
    if not isinstance(name, str):
        err_console.print(f"[red]{pyproject} has no [project] name[/red]")
        raise typer.Exit(1)
    return name


def _run_installer(target: str, *, editable: bool) -> None:
    spec: list[str] = ["-e", str(Path(target).resolve())] if editable else [target]
    if shutil.which("uv") is not None:
        cmd = ["uv", "pip", "install", "--python", sys.executable, *spec]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *spec]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err_console.print(f"[red]install failed:[/red] {' '.join(cmd)}")
        for stream in (result.stderr, result.stdout):
            if stream.strip():
                err_console.print(stream.strip())
        raise typer.Exit(1)
    console.print(f"[green]installed[/green] {spec[-1]}")


def _entry_point_names(dist: str) -> list[str]:
    """Strategy names the installed distribution declares — read in a fresh
    subprocess so the freshly-written dist metadata is seen (the in-process
    importlib.metadata cache would miss it)."""
    want = dist.lower().replace("_", "-")
    snippet = (
        "import json\n"
        "from importlib import metadata as m\n"
        f"print(json.dumps(sorted(ep.name for ep in m.entry_points(group={_ENTRY_GROUP!r})\n"
        "    if ep.dist is not None and (ep.dist.name or '').lower().replace('_', '-')"
        f" == {want!r})))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", snippet], capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        err_console.print(f"[red]could not read the installed entry points:[/red] {exc.stderr}")
        raise typer.Exit(1) from exc
    names: list[str] = json.loads(result.stdout)
    return names


def _append_allowlist(names: list[str], env_path: Path) -> list[str]:
    """Union `names` into JARVIS_STRATEGY_PLUGIN_ALLOWLIST in `.env` (created
    if absent); returns the names that were actually added."""
    existing: list[str] = []
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        for line in lines:
            if line.startswith(f"{ALLOWLIST_VAR}="):
                existing = [v.strip() for v in line.split("=", 1)[1].split(",") if v.strip()]
                break
    added = [n for n in names if n not in existing]
    if not added:
        return []
    new_line = f"{ALLOWLIST_VAR}={','.join(existing + added)}"
    if lines:
        replaced = False
        merged: list[str] = []
        for line in lines:
            if line.startswith(f"{ALLOWLIST_VAR}=") and not replaced:
                merged.append(new_line)
                replaced = True
            else:
                merged.append(line)
        if not replaced:
            merged.append(new_line)
        content = "\n".join(merged) + "\n"
    else:
        content = new_line + "\n"
    env_path.write_text(content)
    return added
