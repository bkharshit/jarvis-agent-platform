"""MCP server management (S4, ADR 0012 §5). Servers are tenant-scoped rows:
CRUD is admin/owner only (anonymous mode is the default tenant's full
access), listing/probing is any tenant member, and a foreign tenant's
server id is 404 — no existence leak (D29). Names are immutable once
created (they join agent version snapshots via `mcp__<name>__<tool>`), so
a delete leaves agents' snapshots intact — the next run fails resolution
honestly with a terminal `tool` failure (D38)."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.deps import AppContainer, get_container
from jarvis.api.errors import ApiError
from jarvis.api.schemas import McpProbeResponse, McpServerCreate, McpServerList, McpServerPatch
from jarvis.domain.mcp import McpServer
from jarvis.tools.mcp.errors import McpResolutionError

router = APIRouter(prefix="/mcp", tags=["mcp"])

# Module-level Depends singleton (ruff B008).
ContainerDep = Depends(get_container)


def _require_server_manager(auth: AuthContext) -> None:
    """Admin/owner only — except anonymous mode, the default tenant's full
    access (the dev walkthrough needs no auth setup)."""
    if auth.user is not None and auth.principal.role not in ("owner", "admin"):
        raise ApiError(403, "forbidden", "MCP server management requires the admin or owner role")


async def _visible_server(auth: AuthContext, container: AppContainer, server_id: str) -> McpServer:
    server = await container.mcp_servers.get(server_id, tenant_id=auth.principal.tenant_id)
    if server is None:
        raise ApiError(404, "not_found", f"MCP server {server_id!r} not found")
    return server


@router.get("/servers")
async def list_servers(
    auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> McpServerList:
    servers = await container.mcp_servers.list_servers(tenant_id=auth.principal.tenant_id)
    return McpServerList(items=servers)


@router.post("/servers", status_code=201)
async def create_server(
    req: McpServerCreate, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> McpServer:
    _require_server_manager(auth)
    try:
        server = McpServer(id=str(uuid4()), name=req.name, config=req.config, enabled=req.enabled)
    except ValidationError as exc:
        raise ApiError(422, "validation", exc.errors()[0]["msg"]) from None
    try:
        return await container.mcp_servers.create(server, tenant_id=auth.principal.tenant_id)
    except IntegrityError:
        raise ApiError(
            409, "conflict", f"an MCP server named {req.name!r} already exists"
        ) from None


@router.get("/servers/{server_id}")
async def get_server(
    server_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> McpServer:
    return await _visible_server(auth, container, server_id)


@router.patch("/servers/{server_id}")
async def update_server(
    server_id: str,
    req: McpServerPatch,
    auth: AuthContext = AuthDep,
    container: AppContainer = ContainerDep,
) -> McpServer:
    _require_server_manager(auth)
    if req.name is not None:
        raise ApiError(
            422,
            "validation",
            "MCP server names are immutable — they join agent version snapshots "
            "(delete the server and create a new one instead)",
        )
    current = await _visible_server(auth, container, server_id)
    updated = current.model_copy(
        update={
            "enabled": req.enabled if req.enabled is not None else current.enabled,
            "config": req.config if req.config is not None else current.config,
        }
    )
    try:
        return await container.mcp_servers.update(updated, tenant_id=auth.principal.tenant_id)
    except LookupError:
        raise ApiError(404, "not_found", f"MCP server {server_id!r} not found") from None


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(
    server_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> None:
    _require_server_manager(auth)
    deleted = await container.mcp_servers.delete(server_id, tenant_id=auth.principal.tenant_id)
    if not deleted:
        raise ApiError(404, "not_found", f"MCP server {server_id!r} not found")


@router.post("/servers/{server_id}/probe")
async def probe_server(
    server_id: str, auth: AuthContext = AuthDep, container: AppContainer = ContainerDep
) -> McpProbeResponse:
    """Connect fresh through the provider's connection path — no registry
    side effects, nothing persisted. Any member may probe; a resolution
    failure is a 502 in the frozen error envelope (the server is reachable
    enough to exist but not to talk to)."""
    server = await _visible_server(auth, container, server_id)
    try:
        # The requesting principal rides along: stored header refs (ADR
        # 0013) decrypt tenant-scoped for whoever probes — a member probing
        # the same server gets the same behavior as an admin.
        tools = await container.mcp.probe(server, principal=auth.principal)
    except McpResolutionError as exc:
        raise ApiError(502, "mcp_unreachable", str(exc)) from None
    return McpProbeResponse(server=server, tools=tools)


__all__ = ["router"]
