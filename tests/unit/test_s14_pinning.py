"""S14 commit 6 — publish pinning of `sub__`/`handoff__` bindings (D51):
server-stamped pins (client pins overwritten), the 422s (no agent_id,
self-binding, unpublished/foreign target, cycles over pinned snapshots),
and the agent-detail lints (stale pins, memory-on-child). The route
helpers are tested directly against a fake repo view — the HTTP layer
(exactly-once publish, envelopes) is the integration suite's."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.api.errors import ApiError
from jarvis.api.routes.agents import _ensure_acyclic, _lints, _pin_family_bindings
from jarvis.domain.agent import AgentDefinition, AgentVersion, ModelRef, StrategyConfig, ToolBinding

# --- the doubles ---------------------------------------------------------------


def _knowledge_stub():
    """The S8 repo seam — no S14 definition carries a knowledge binding,
    so the stub's dataset is never fetched."""

    async def _missing(dataset_id, *, tenant_id):
        return None

    return SimpleNamespace(get_dataset=_missing)


class _RepoView:
    """The TenantScopedAgents slice the pin helpers read."""

    def __init__(self, versions: list[AgentVersion]):
        self._by_agent: dict[str, AgentVersion] = {v.agent_id: v for v in versions}
        self._by_id: dict[str, AgentVersion] = {v.id: v for v in versions}

    async def latest_version(self, agent_id: str):
        return self._by_agent.get(agent_id)

    async def get_version_by_id(self, version_id: str):
        return self._by_id.get(version_id)


def _auth(versions: list[AgentVersion]):
    # principal.tenant_id rides the S8 knowledge lints (unreached here —
    # no S14 definition carries a knowledge binding).
    return SimpleNamespace(agents=_RepoView(versions), principal=SimpleNamespace(tenant_id="t1"))


def _version(
    agent_id: str, tools: list[ToolBinding], *, version_id: str | None = None
) -> AgentVersion:
    snapshot = AgentDefinition(
        id=agent_id,
        name=agent_id,
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        tools=tools,
    )
    return AgentVersion(
        id=version_id or f"ver-{agent_id}", agent_id=agent_id, version=1, snapshot=snapshot
    )


def _definition(agent_id: str, tools: list[ToolBinding], **kw) -> AgentDefinition:
    base = dict(
        id=agent_id,
        name=agent_id,
        model=ModelRef(provider="mock", model="mock-1"),
        strategy=StrategyConfig(type="function_calling"),
        tools=tools,
    )
    base.update(kw)
    return AgentDefinition(**base)


_SUB_A = ToolBinding(name="sub__b", config={"agent_id": "b", "agent_version_id": "ver-b"})
_SUB_A_NO_PIN = ToolBinding(name="sub__b", config={"agent_id": "b"})
_SUB_B = ToolBinding(name="sub__a", config={"agent_id": "a", "agent_version_id": "ver-a"})
_CALC = ToolBinding(name="calculator")


# --- the pin overwrite ---------------------------------------------------------


class TestPinOverwrite:
    async def test_client_pins_are_overwritten_never_trusted(self):
        definition = _definition(
            "a",
            [
                ToolBinding(
                    name="sub__b", config={"agent_id": "b", "agent_version_id": "client-garbage"}
                )
            ],
        )
        pinned = await _pin_family_bindings(_auth([_version("b", [])]), "a", definition)
        assert pinned.tools[0].config["agent_version_id"] == "ver-b"
        # the rest of the config survives the overwrite
        assert pinned.tools[0].config["agent_id"] == "b"

    async def test_no_family_bindings_returns_the_definition_as_is(self):
        definition = _definition("a", [_CALC])
        assert await _pin_family_bindings(_auth([]), "a", definition) is definition

    async def test_calculator_binding_rides_along_unchanged(self):
        definition = _definition("a", [_CALC, ToolBinding(name="sub__b", config={"agent_id": "b"})])
        pinned = await _pin_family_bindings(_auth([_version("b", [])]), "a", definition)
        assert pinned.tools[0] is not None
        assert pinned.tools[0].config == _CALC.config
        assert pinned.tools[1].config["agent_version_id"] == "ver-b"


class TestPin422s:
    async def test_binding_without_agent_id(self):
        definition = _definition("a", [ToolBinding(name="sub__b")])
        with pytest.raises(ApiError) as excinfo:
            await _pin_family_bindings(_auth([]), "a", definition)
        assert excinfo.value.status_code == 422
        assert "sub__b" in excinfo.value.message

    async def test_direct_self_binding(self):
        definition = _definition("a", [ToolBinding(name="sub__a", config={"agent_id": "a"})])
        with pytest.raises(ApiError, match="binds agent 'a' to itself"):
            await _pin_family_bindings(_auth([]), "a", definition)

    async def test_unpublished_or_foreign_target(self):
        # D29: the tenant-scoped read makes a foreign target read as absent —
        # the same 422 either way.
        definition = _definition(
            "a", [ToolBinding(name="sub__ghost", config={"agent_id": "ghost"})]
        )
        with pytest.raises(ApiError, match="ghost"):
            await _pin_family_bindings(_auth([]), "a", definition)


# --- the cycle walk --------------------------------------------------------------


class TestCycleWalk:
    async def test_acyclic_chain_passes(self):
        # a → b → c, all fine
        definition = _definition("a", [_SUB_A])
        auth = _auth(
            [
                _version(
                    "b",
                    [
                        ToolBinding(
                            name="sub__c", config={"agent_id": "c", "agent_version_id": "ver-c"}
                        )
                    ],
                )
            ]
        )
        await _ensure_acyclic(auth, definition)

    async def test_two_node_cycle_422s(self):
        # a → b, and b's PINNED snapshot binds back to a's (old, published)
        # version — the A→B→A publish 422s
        definition = _definition("a", [_SUB_A])
        auth = _auth([_version("a", []), _version("b", [_SUB_B])])
        with pytest.raises(ApiError, match="cycle through 'a'"):
            await _ensure_acyclic(auth, definition)

    async def test_cycle_through_a_deeper_node_422s(self):
        # a → b → c → b
        definition = _definition("a", [_SUB_A])
        auth = _auth(
            [
                _version(
                    "b",
                    [
                        ToolBinding(
                            name="sub__c", config={"agent_id": "c", "agent_version_id": "ver-c"}
                        )
                    ],
                ),
                _version(
                    "c",
                    [
                        ToolBinding(
                            name="sub__b", config={"agent_id": "b", "agent_version_id": "ver-b"}
                        )
                    ],
                ),
            ]
        )
        with pytest.raises(ApiError, match="cycle through 'b'"):
            await _ensure_acyclic(auth, definition)

    async def test_diamond_is_not_a_cycle(self):
        # a → b, a → c, b → d, c → d — revisiting a DONE node is fine
        definition = _definition(
            "a",
            [
                ToolBinding(name="sub__b", config={"agent_id": "b", "agent_version_id": "ver-b"}),
                ToolBinding(name="sub__c", config={"agent_id": "c", "agent_version_id": "ver-c"}),
            ],
        )
        sub_d = ToolBinding(name="sub__d", config={"agent_id": "d", "agent_version_id": "ver-d"})
        auth = _auth([_version("b", [sub_d]), _version("c", [sub_d])])
        await _ensure_acyclic(auth, definition)

    async def test_walk_tolerates_a_broken_pin(self):
        # a stale/broken pin ends the walk (the lint names it) — never a
        # spurious cycle verdict
        definition = _definition("a", [_SUB_A_NO_PIN])
        await _ensure_acyclic(_auth([]), definition)


# --- the detail lints -------------------------------------------------------------


class TestLints:
    async def test_no_family_bindings_no_lints(self):
        lints = await _lints(_auth([]), _definition("a", [_CALC]), _knowledge_stub())
        assert lints == []

    async def test_stale_pin_is_a_lint_never_a_failure(self):
        # the pin names an OLD version while a newer one is published
        stale = AgentVersion(
            id="ver-b-old",
            agent_id="b",
            version=1,
            snapshot=_definition("b", []),
        )
        definition = _definition(
            "a",
            [ToolBinding(name="sub__b", config={"agent_id": "b", "agent_version_id": "ver-b-old"})],
        )
        lints = await _lints(
            _auth([stale, _version("b", [], version_id="ver-b-new")]), definition, _knowledge_stub()
        )
        assert len(lints) == 1
        assert "stale" in lints[0] and "sub__b" in lints[0]

    async def test_target_with_no_published_version_is_a_lint(self):
        definition = _definition(
            "a",
            [
                ToolBinding(
                    name="sub__ghost", config={"agent_id": "ghost", "agent_version_id": "ver-g"}
                )
            ],
        )
        lints = await _lints(_auth([]), definition, _knowledge_stub())
        assert len(lints) == 1
        assert "ghost" in lints[0]

    async def test_memory_on_child_is_a_lint(self):
        child = AgentDefinition(
            id="b",
            name="b",
            model=ModelRef(provider="mock", model="mock-1"),
            strategy=StrategyConfig(type="function_calling"),
            memory={"enabled": True},
        )
        version = AgentVersion(id="ver-b", agent_id="b", version=1, snapshot=child)
        definition = _definition(
            "a", [ToolBinding(name="sub__b", config={"agent_id": "b", "agent_version_id": "ver-b"})]
        )
        lints = await _lints(_auth([version]), definition, _knowledge_stub())
        assert lints == [
            "tool 'sub__b' pins memory-enabled agent 'b' — children run "
            "memory-less (no shared session)"
        ]

    async def test_fresh_pin_and_memoryless_child_are_silent(self):
        definition = _definition(
            "a", [ToolBinding(name="sub__b", config={"agent_id": "b", "agent_version_id": "ver-b"})]
        )
        lints = await _lints(_auth([_version("b", [])]), definition, _knowledge_stub())
        assert lints == []

    async def test_binding_without_agent_id_is_a_lint(self):
        definition = _definition("a", [ToolBinding(name="sub__b")])
        lints = await _lints(_auth([]), definition, _knowledge_stub())
        assert lints == ["tool 'sub__b' carries no agent_id in its config"]
