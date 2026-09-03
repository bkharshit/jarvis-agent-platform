"""`jarvis` CLI (plan §6) — Typer + rich, reusing the AppContainer so the
CLI never re-implements runtime logic: the backend exists independently of
any UI, and the CLI proves it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from jarvis import __version__
from jarvis.api.deps import AppContainer
from jarvis.config import Settings
from jarvis.domain.agent import AgentDefinition
from jarvis.domain.events import (
    ExecutionEvent,
    IterationStarted,
    RunCancelled,
    RunCompleted,
    RunFailed,
    RunStarted,
    TextDelta,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
    ToolCallStarted,
)
from jarvis.domain.message import user as user_message

app = typer.Typer(no_args_is_help=True, help="JARVIS agent platform CLI")
agent_app = typer.Typer(no_args_is_help=True, help="Manage agents")
executions_app = typer.Typer(no_args_is_help=True, help="Inspect executions")
app.add_typer(agent_app, name="agent")
app.add_typer(executions_app, name="executions")

console = Console()
err_console = Console(stderr=True)

# Agent-definition keys the server owns (never taken from a --file payload).
_SERVER_OWNED = {"id", "created_at", "updated_at"}


def _settings() -> Settings:
    return Settings()


def _container() -> AppContainer:
    return AppContainer.from_settings(_settings())


async def _with_container(action: Any) -> None:
    container = _container()
    try:
        await action(container)
    finally:
        await container.aclose()


def _definition_from_file(path: Path) -> dict[str, Any]:
    import yaml

    payload = yaml.safe_load(path.read_text())
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{path} must contain a YAML mapping")
    for key in _SERVER_OWNED:
        payload.pop(key, None)
    return payload


def _print_event(event: ExecutionEvent) -> None:
    if isinstance(event, TextDelta):
        console.print(event.text, end="")
        return
    detail = ""
    if isinstance(event, RunStarted):
        detail = f"input={event.input!r}"
    elif isinstance(event, IterationStarted):
        detail = f"iteration={event.iteration}"
    elif isinstance(
        event, (ToolCallRequested, ToolCallStarted, ToolCallCompleted, ToolCallFailed)
    ):
        detail = f"tool={event.name}"
        if isinstance(event, ToolCallCompleted):
            detail += f" ok={not event.is_error} latency={event.latency_ms}ms"
        elif isinstance(event, ToolCallFailed):
            detail += f" error={event.error!r}"
    elif isinstance(event, RunCompleted):
        detail = f"iterations={event.iterations}"
    elif isinstance(event, RunFailed):
        detail = f"error_kind={event.error_kind} error={event.error!r}"
    elif isinstance(event, RunCancelled):
        detail = f"reason={event.reason!r}"
    console.print(f"[dim]{event.sequence}[/dim] [bold]{event.type}[/bold] {detail}")


# --- init / serve / doctor / version -----------------------------------------


@app.command()
def init() -> None:
    """Create the schema (alembic upgrade head) against JARVIS_DATABASE_URL."""
    from alembic import command
    from alembic.config import Config

    import jarvis.persistence

    settings = _settings()
    config = Config()
    # Programmatic script_location so this works from an installed package.
    config.set_main_option(
        "script_location",
        str(Path(jarvis.persistence.__file__).parent / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    console.print("[green]Schema is up to date.[/green]")


@app.command()
def serve() -> None:
    """Run the HTTP API (host/port from JARVIS_HOST / JARVIS_PORT)."""
    import uvicorn

    settings = _settings()
    console.print(f"Serving on http://{settings.host}:{settings.port}")
    uvicorn.run("jarvis.api.app:create_app", factory=True, host=settings.host, port=settings.port)


@app.command()
def version() -> None:
    """Print the JARVIS version."""
    console.print(f"jarvis {__version__}")


@app.command()
def doctor(
    ping_model: bool = typer.Option(False, "--ping-model", help="Send a tiny model request."),
) -> None:
    """Check configuration, DB connectivity, and provider setup."""

    async def check(container: AppContainer) -> None:
        tree = Tree("doctor")
        try:
            async with container.engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
            db_note = "[green]ok[/green]"
        except Exception as exc:  # noqa: BLE001 — doctor reports, never raises
            db_note = f"[red]failed: {type(exc).__name__}: {exc}[/red]"
        tree.add(f"database: {db_note}")

        settings = container.settings
        tree.add(f"model provider: {settings.model_provider} / {settings.model_name}")

        if ping_model:
            from jarvis.domain.agent import ModelRef
            from jarvis.models.types import ModelRequest

            ref = ModelRef(
                provider=settings.model_provider,
                model=settings.model_name,
                base_url=settings.model_base_url,
                api_key_env=settings.model_api_key_env,
            )
            try:
                client = container.models.resolve(ref)
                request = ModelRequest(
                    model=ref.model, messages=[user_message("Reply with: ok")]
                )
                response = await client.generate(request)
                model_note = f"[green]ok[/green]: {response.message.text[:80]!r}"
            except Exception as exc:  # noqa: BLE001
                model_note = f"[red]failed: {type(exc).__name__}: {exc}[/red]"
            tree.add(f"model ping: {model_note}")

        tree.add(
            "tools: " + ", ".join(sorted(d.name for d in container.tools.descriptors()))
        )
        console.print(tree)

    asyncio.run(_with_container(check))


# --- agent ---------------------------------------------------------------


@agent_app.command("create")
def agent_create(
    file: Path = typer.Option(..., "--file", help="Agent definition YAML."),
) -> None:
    """Create an agent from a YAML definition (publishes version 1)."""

    async def action(container: AppContainer) -> None:
        from uuid import uuid4

        payload = _definition_from_file(file)
        definition = AgentDefinition(id=str(uuid4()), **payload)
        await container.agents.create(definition)
        console.print(f"[green]created[/green] {definition.name} ({definition.id}) v1")

    asyncio.run(_with_container(action))


@agent_app.command("list")
def agent_list() -> None:
    """List agents."""

    async def action(container: AppContainer) -> None:
        agents = await container.agents.list_agents()
        table = Table(title="agents")
        table.add_column("id")
        table.add_column("name")
        table.add_column("model")
        table.add_column("strategy")
        for definition in agents:
            table.add_row(
                definition.id, definition.name,
                f"{definition.model.provider}/{definition.model.model}",
                definition.strategy.type,
            )
        console.print(table)

    asyncio.run(_with_container(action))


@agent_app.command("show")
def agent_show(
    name: str,
    version: int | None = typer.Option(None, "-v", "--version", help="Frozen snapshot to show."),
) -> None:
    """Show an agent by name (current definition, or a frozen version)."""

    async def action(container: AppContainer) -> None:
        definition = await container.agents.get_by_name(name)
        if definition is None:
            err_console.print(f"[red]agent {name!r} not found[/red]")
            raise typer.Exit(1)
        shown: AgentDefinition = definition
        if version is not None:
            snapshot = await container.agents.get_version(definition.id, version)
            if snapshot is None:
                err_console.print(f"[red]version {version} not found[/red]")
                raise typer.Exit(1)
            shown = snapshot.snapshot
        console.print(json.dumps(shown.model_dump(mode="json"), indent=2, default=str))

    asyncio.run(_with_container(action))


@agent_app.command("delete")
def agent_delete(name: str) -> None:
    """Delete an agent (refused while executions exist)."""

    async def action(container: AppContainer) -> None:
        definition = await container.agents.get_by_name(name)
        if definition is None:
            err_console.print(f"[red]agent {name!r} not found[/red]")
            raise typer.Exit(1)
        if not await container.agents.delete(definition.id):
            err_console.print(
                f"[red]agent {name!r} has executions; delete refused[/red]"
            )
            raise typer.Exit(1)
        console.print(f"[green]deleted[/green] {name}")

    asyncio.run(_with_container(action))


# --- run -----------------------------------------------------------------


@app.command()
def run(
    name: str,
    input: str,
    session: str | None = typer.Option(None, "--session", help="Session id (enables memory)."),
    stream: bool = typer.Option(False, "--stream", help="Stream events live."),
    variables: list[str] = typer.Option(
        [], "--vars", help="Template variables as key=value (repeatable)."
    ),
) -> None:
    """Run an agent by name; print the final message or a live event stream."""

    async def action(container: AppContainer) -> None:
        definition = await container.agents.get_by_name(name)
        if definition is None:
            err_console.print(f"[red]agent {name!r} not found[/red]")
            raise typer.Exit(1)
        version = await container.agents.latest_version(definition.id)
        if version is None:
            err_console.print(f"[red]agent {name!r} has no published version[/red]")
            raise typer.Exit(1)
        from uuid import uuid4

        from jarvis.domain.execution import ExecutionContext
        from jarvis.runtime.limits import deadline_from_now

        settings = _settings()
        vars_dict: dict[str, Any] = {}
        for pair in variables:
            key, _, value = pair.partition("=")
            vars_dict[key] = value
        ctx = ExecutionContext(
            run_id=str(uuid4()),
            agent_id=definition.id,
            agent_version_id=version.id,
            session_id=session,
            trace_id=str(uuid4()),
            variables=vars_dict,
            deadline=deadline_from_now(settings.run_timeout_seconds),
        )
        if stream:
            sink = await container.bus.get_or_create(ctx.run_id)
            run_task = asyncio.create_task(
                container.runtime.run(version, input, ctx, sink=sink)
            )
            async for _, event in sink.subscribe(None):
                _print_event(event)
            result = await run_task
            console.print()  # newline after streamed text deltas
        else:
            result = await container.runtime.run(version, input, ctx)
        console.print(f"[dim]run {result.run_id} -> {result.status}[/dim]")
        if result.error:
            err_console.print(f"[red]{result.error}[/red]")
            raise typer.Exit(1)

    asyncio.run(_with_container(action))


# --- executions ----------------------------------------------------------


@executions_app.command("list")
def executions_list(
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent name."),
) -> None:
    """List recent executions."""

    async def action(container: AppContainer) -> None:
        agent_id: str | None = None
        if agent is not None:
            definition = await container.agents.get_by_name(agent)
            if definition is None:
                err_console.print(f"[red]agent {agent!r} not found[/red]")
                raise typer.Exit(1)
            agent_id = definition.id
        runs = await container.executions.list_runs(agent_id=agent_id)
        table = Table(title="executions")
        table.add_column("run_id")
        table.add_column("agent_id")
        table.add_column("status")
        table.add_column("iterations")
        table.add_column("error")
        for run in runs:
            table.add_row(
                run.run_id, run.agent_id, run.status, str(run.iterations), run.error or ""
            )
        console.print(table)

    asyncio.run(_with_container(action))


@executions_app.command("show")
def executions_show(
    run_id: str,
    events: bool = typer.Option(False, "--events", help="Replay the run's event log."),
) -> None:
    """Show one execution (detail + transcript, optionally its events)."""

    async def action(container: AppContainer) -> None:
        run = await container.executions.get(run_id)
        if run is None:
            err_console.print(f"[red]execution {run_id!r} not found[/red]")
            raise typer.Exit(1)
        console.print(json.dumps(run.model_dump(mode="json"), indent=2, default=str))
        if events:
            async for event in container.executions.list_events(run_id):
                _print_event(event)
            console.print()

    asyncio.run(_with_container(action))


if __name__ == "__main__":
    app()
