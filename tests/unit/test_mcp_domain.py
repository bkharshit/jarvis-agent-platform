"""McpServer domain validation (S4, ADR 0012 §1/D37) — hermetic."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from jarvis.domain.agent import EnvCredentialRef, StoredCredentialRef
from jarvis.domain.mcp import McpHttpConfig, McpServer, McpStdioConfig


def _stdio(**overrides: object) -> dict:
    payload: dict = {"type": "stdio", "command": "python", "args": ["-m", "weather"]}
    payload.update(overrides)
    return payload


def _http(**overrides: object) -> dict:
    payload: dict = {"type": "http", "url": "https://example.com/mcp"}
    payload.update(overrides)
    return payload


def test_stdio_config_roundtrip() -> None:
    config = McpStdioConfig.model_validate(_stdio())
    assert config.command == "python"
    assert config.args == ["-m", "weather"]
    assert config.env == {}


def test_stdio_env_refs_carry_names_not_values() -> None:
    config = McpStdioConfig.model_validate(
        _stdio(env={"WEATHER_TOKEN": {"type": "env", "env_var": "MCP_WEATHER_TOKEN"}})
    )
    assert isinstance(config.env["WEATHER_TOKEN"], EnvCredentialRef)
    assert config.env["WEATHER_TOKEN"].env_var == "MCP_WEATHER_TOKEN"


def test_stdio_env_refs_accept_stored_credential_ids() -> None:
    config = McpStdioConfig.model_validate(
        _stdio(env={"WEATHER_TOKEN": {"type": "stored", "credential_id": "cred-1"}})
    )
    assert isinstance(config.env["WEATHER_TOKEN"], StoredCredentialRef)
    assert config.env["WEATHER_TOKEN"].credential_id == "cred-1"


def test_http_headers_carry_stored_credential_refs(  # ADR 0013 §1
) -> None:
    config = McpHttpConfig.model_validate(
        _http(headers={"Authorization": {"type": "stored", "credential_id": "cred-1"}})
    )
    assert isinstance(config.headers["Authorization"], StoredCredentialRef)
    assert config.headers["Authorization"].credential_id == "cred-1"


def test_credential_ref_union_is_strict() -> None:
    # extra="forbid" on both variants: a ref mixing shapes is invalid, not
    # silently accepted.
    with pytest.raises(ValidationError):
        McpHttpConfig.model_validate(
            _http(
                headers={"Authorization": {"type": "stored", "credential_id": "x", "env_var": "y"}}
            )
        )
    with pytest.raises(ValidationError):
        McpHttpConfig.model_validate(_http(headers={"Authorization": {"type": "env"}}))


def test_config_union_discriminates_on_type() -> None:
    server = McpServer(id=str(uuid4()), name="weather", config=_stdio())  # type: ignore[arg-type]
    assert isinstance(server.config, McpStdioConfig)
    server = McpServer(id=str(uuid4()), name="weather", config=_http())  # type: ignore[arg-type]
    assert isinstance(server.config, McpHttpConfig)


def test_unknown_config_type_rejected() -> None:
    with pytest.raises(ValidationError):
        McpStdioConfig.model_validate({"type": "websocket", "url": "wss://x"})


def test_http_url_must_be_http_s() -> None:
    with pytest.raises(ValidationError, match="http"):
        McpHttpConfig.model_validate({"type": "http", "url": "ftp://example.com/mcp"})
    with pytest.raises(ValidationError, match="http"):
        McpHttpConfig.model_validate({"type": "http", "url": "not-a-url"})


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError, match="extra"):
        McpStdioConfig.model_validate(_stdio(transport="magic"))


@pytest.mark.parametrize("name", ["fixtures", "weather-2", "0server", "a-b-c", "trailing-"])
def test_slug_names_accepted(name: str) -> None:
    # A trailing hyphen matches the ADR 0012 pattern as written
    # (`^[a-z0-9][a-z0-9-]*$`) — the regex is the contract, not intuition.
    server = McpServer(id=str(uuid4()), name=name, config=_stdio())  # type: ignore[arg-type]
    assert server.name == name
    assert server.enabled is True
    assert server.tenant_id is None


@pytest.mark.parametrize("name", ["", "Weather", "-leading", "has space", "a_b", "mcp__x"])
def test_slug_names_rejected(name: str) -> None:
    with pytest.raises(ValidationError, match="slug"):
        McpServer(id=str(uuid4()), name=name, config=_stdio())  # type: ignore[arg-type]


def test_empty_command_rejected() -> None:
    with pytest.raises(ValidationError):
        McpStdioConfig.model_validate(_stdio(command=""))
