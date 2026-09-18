"""Repository protocols — Protocols ONLY.

The Pydantic ↔ JSONB serialization boundary lives at the implementation
edge (ADR 0002). `update_and_publish` appends a new immutable AgentVersion
and repoints `agents.current_version` — history is never rewritten.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from jarvis.domain.agent import (
    AgentDefinition,
    AgentVersion,
    ConversationMemoryState,
    ScratchpadEntry,
)
from jarvis.domain.evaluation import (
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
    Score,
)
from jarvis.domain.events import ExecutionEvent
from jarvis.domain.execution import ExecutionStatus, RunResult
from jarvis.domain.knowledge import (
    KnowledgeChunk,
    KnowledgeDataset,
    KnowledgeDocument,
)
from jarvis.domain.mcp import McpServer
from jarvis.domain.message import Message, Usage
from jarvis.domain.tools import ToolResult
from jarvis.domain.trigger import Trigger
from jarvis.domain.workflow import WorkflowDefinition, WorkflowVersion


class AgentRepo(Protocol):
    async def create(self, definition: AgentDefinition) -> AgentDefinition:
        """Create the agent and publish its first version."""
        ...

    async def get(self, agent_id: str) -> AgentDefinition | None: ...

    async def get_by_name(self, name: str) -> AgentDefinition | None: ...

    async def list_agents(self, limit: int = 50, offset: int = 0) -> list[AgentDefinition]: ...

    async def update_and_publish(
        self, definition: AgentDefinition, label: str = ""
    ) -> AgentVersion:
        """Update mutable fields and append a new immutable snapshot."""
        ...

    async def get_version(self, agent_id: str, version: int) -> AgentVersion | None: ...

    async def latest_version(self, agent_id: str) -> AgentVersion | None: ...

    async def list_versions(self, agent_id: str) -> list[AgentVersion]: ...

    async def delete(self, agent_id: str) -> bool:
        """Returns False if the agent has executions (caller maps to 409)."""
        ...

    async def has_executions(self, agent_id: str) -> bool: ...


class WorkflowRepo(Protocol):
    """Workflows + append-only version history (S6, ADR 0015) — the exact
    AgentRepo shape one level up: same pointer row / immutable snapshot
    split, same tenancy (NULL = shared), same delete-with-executions
    refusal. `get_version_by_id` is how the worker resolves the workflow
    version pinned on a queue message (the AgentVersionLoader pattern)."""

    async def create(
        self, definition: WorkflowDefinition, *, tenant_id: str | None = None
    ) -> WorkflowDefinition:
        """Create the workflow and publish its first version."""
        ...

    async def get(
        self, workflow_id: str, *, tenant_id: str | None = None
    ) -> WorkflowDefinition | None: ...

    async def get_by_name(
        self, name: str, *, tenant_id: str | None = None
    ) -> WorkflowDefinition | None: ...

    async def list_workflows(
        self, limit: int = 50, offset: int = 0, *, tenant_id: str | None = None
    ) -> list[WorkflowDefinition]: ...

    async def update_and_publish(
        self, definition: WorkflowDefinition, label: str = "", *, tenant_id: str | None = None
    ) -> WorkflowVersion:
        """Update mutable fields, pin agent-node versions (D42), append a new
        immutable snapshot."""
        ...

    async def get_version(
        self, workflow_id: str, version: int, *, tenant_id: str | None = None
    ) -> WorkflowVersion | None: ...

    async def get_version_by_id(self, version_id: str) -> WorkflowVersion | None: ...

    async def latest_version(
        self, workflow_id: str, *, tenant_id: str | None = None
    ) -> WorkflowVersion | None: ...

    async def list_versions(
        self, workflow_id: str, *, tenant_id: str | None = None
    ) -> list[WorkflowVersion]: ...

    async def delete(self, workflow_id: str, *, tenant_id: str | None = None) -> bool:
        """Returns False if the workflow has executions (caller maps to 409)."""
        ...

    async def has_executions(self, workflow_id: str) -> bool: ...


class ExecutionRepo(Protocol):
    async def create_run(self, result: RunResult) -> None: ...

    async def finish_run(self, result: RunResult) -> None: ...

    async def mark_awaiting_input(
        self, run_id: str, awaiting_until: datetime, *, total_usage: Usage | None = None
    ) -> None:
        """running → awaiting_input with the pause deadline (S10, ADR 0010).
        `total_usage` lands on the row too: the resume segment re-seeds its
        usage budget from the row, so the pause must write the chain's
        usage-so-far (the terminal write is the only other one)."""
        ...

    async def get(self, run_id: str) -> RunResult | None: ...

    async def list_runs(
        self,
        agent_id: str | None = None,
        status: ExecutionStatus | None = None,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        parent_run_id: str | None = None,
        trigger_id: str | None = None,
    ) -> list[RunResult]:
        """`parent_run_id` (S14, ADR 0018 §3): equality filter over the
        nullable parent column — a run's children, or None-filtered for
        root runs when the API needs it.
        `trigger_id` (S13, ADR 0019 §6): equality filter over the nullable
        provenance column — the runs a trigger fired."""
        ...

    async def save_message(self, run_id: str, message: Message) -> None: ...

    async def list_messages(self, run_id: str) -> list[Message]: ...

    async def save_tool_execution(
        self, run_id: str, result: ToolResult, arguments: dict[str, object]
    ) -> None: ...

    async def list_tool_executions(self, run_id: str) -> list[ToolResult]: ...

    async def append_event(self, event: ExecutionEvent) -> int:
        """Persist with the sink-assigned per-run sequence; return the
        global cursor (SSE Last-Event-ID)."""
        ...

    def list_events(self, run_id: str, after: int | None = None) -> AsyncIterator[ExecutionEvent]:
        """Async-generator replay (per-run `sequence` order)."""
        ...


class ConversationRepo(Protocol):
    async def get_or_create(
        self, agent_id: str, session_id: str, *, tenant_id: str | None = None
    ) -> str:
        """`tenant_id` stamps the owning tenant (S2, ADR 0009); None keeps
        the pre-S2 default-tenant behavior."""
        ...

    async def find(self, agent_id: str, session_id: str) -> str | None:
        """Look up without creating — the read-side pair of get_or_create."""
        ...

    async def append_message(
        self, conversation_id: str, message: Message, run_id: str | None = None
    ) -> int:
        """Returns the message's conversation sequence."""
        ...

    async def history(self, conversation_id: str, limit: int | None = None) -> list[Message]: ...

    async def get_summary_state(
        self, conversation_id: str, *, tenant_id: str | None = None
    ) -> ConversationMemoryState | None:
        """The rolling-summary state (S12, D45). None when the conversation
        does not exist; a zero state means "nothing summarized yet"."""
        ...

    async def save_summary(
        self,
        conversation_id: str,
        *,
        summary: str,
        summarized_count: int,
        tenant_id: str | None = None,
    ) -> None:
        """Persist the compaction result (S12, D45). `tenant_id` scopes the
        write to the owning conversation when supplied (D29)."""
        ...


class ScratchpadRepo(Protocol):
    """Working-memory KV store (S12, ADR 0016 §3, D46) — keyed
    (agent_id, session_id, key); tenant-scoped like every repo (D29:
    foreign rows read as absent, never leaked)."""

    async def get(
        self, agent_id: str, session_id: str, key: str, *, tenant_id: str | None = None
    ) -> ScratchpadEntry | None: ...

    async def put(
        self,
        agent_id: str,
        session_id: str,
        key: str,
        value: str,
        *,
        tenant_id: str | None = None,
    ) -> ScratchpadEntry:
        """Upsert — the newest write wins; returns the stored entry."""
        ...

    async def delete(
        self, agent_id: str, session_id: str, key: str, *, tenant_id: str | None = None
    ) -> bool:
        """True when a row was removed, False when the key was not set."""
        ...


class McpServerRepo(Protocol):
    """Configured MCP servers (S4, ADR 0012) — tenant-scoped like
    AgentRepo: `tenant_id=None` reads include platform-shared rows
    (tenant_id NULL); writes stamp the given tenant, None = shared."""

    async def create(self, server: McpServer, *, tenant_id: str | None = None) -> McpServer:
        """Create the row; the returned server carries the stamped tenant."""
        ...

    async def get(self, server_id: str, *, tenant_id: str | None = None) -> McpServer | None: ...

    async def get_by_name(self, name: str, *, tenant_id: str | None = None) -> McpServer | None:
        """Tenant-owned rows shadow same-name shared rows (D37)."""
        ...

    async def list_servers(self, *, tenant_id: str | None = None) -> list[McpServer]: ...

    async def update(self, server: McpServer, *, tenant_id: str | None = None) -> McpServer:
        """Mutable fields are `enabled` and `config`; the name is immutable
        (it is the join key from version snapshots)."""
        ...

    async def delete(self, server_id: str, *, tenant_id: str | None = None) -> bool: ...


class TriggerRepo(Protocol):
    """Trigger rules (S13, ADR 0019 §1) — strictly tenant-owned rows, no
    platform-shared tier: explicit REQUIRED `tenant_id` kwargs (the
    McpServerRepo pattern minus shared rows; D29: a foreign id reads as
    absent, never leaked). Rows are mutable (no snapshot discipline — the
    fired runs + their pinned versions are the audit trail)."""

    async def create(self, trigger: Trigger, *, tenant_id: str) -> Trigger:
        """Create the row; the returned trigger carries the stamped tenant."""
        ...

    async def get(self, trigger_id: str, *, tenant_id: str) -> Trigger | None: ...

    async def get_unscoped(self, trigger_id: str) -> Trigger | None:
        """The unauthenticated webhook ingest's ONLY lookup (ADR 0019 §4):
        no tenant kwarg because the ingest route never derives tenant from
        the request — the trigger row + its HMAC secret are the trust
        boundary. Never use it where a principal exists."""
        ...

    async def list_triggers(self, *, tenant_id: str) -> list[Trigger]: ...

    async def update(self, trigger: Trigger, *, tenant_id: str) -> Trigger:
        """Replace the mutable fields of the existing row; None when absent.
        The caller owns schedule math (next_fire_at on create/enable)."""
        ...

    async def delete(self, trigger_id: str, *, tenant_id: str) -> bool:
        """Always deletable — no-FK provenance keeps past runs' trigger_id
        intact (ADR 0019 §6; the eval 409-with-runs was rejected)."""
        ...

    async def claim_due_crons(self, now: datetime, limit: int = 10) -> list[Trigger]:
        """Advance due cron triggers (D59): one transaction — SELECT due
        `kind='cron' AND enabled` rows FOR UPDATE SKIP LOCKED, then advance
        each (`last_fired_at=now`, `next_fire_at=croniter_next(now)`,
        `last_error=NULL`) and COMMIT. Two racing claimants: the row lock
        makes exactly one win (the queue-claim idiom). The enqueue happens
        AFTER this commit — a crash in between misses the fire honestly."""
        ...

    async def record_fire(
        self, trigger_id: str, *, tenant_id: str, fired_at: datetime, error: str | None = None
    ) -> None:
        """Stamp `last_fired_at` (+ `last_error` when a fire's enqueue or
        evaluation failed — the honest record, never a retry storm)."""
        ...

    async def enabled_event_triggers(self, tenant_id: str) -> list[Trigger]:
        """Enabled `kind='event'` rows of the tenant — the finalize observer's
        candidates (D61; matching happens at the service)."""
        ...


class KnowledgeRepo(Protocol):
    """Knowledge datasets (S8, ADR 0020 §1, D63) — strictly tenant-owned
    rows (the TriggerRepo shape: explicit REQUIRED `tenant_id` kwargs; D29:
    a foreign id reads as absent, never leaked). Documents and chunks carry
    NO tenant column — every document/chunk read joins through the
    tenant-scoped dataset (the eval_results precedent), and `search_chunks`
    trusts its caller to pass ONLY tenant-visible dataset ids (the
    KnowledgeRetrieval service resolves them scoped first)."""

    async def create_dataset(
        self, dataset: KnowledgeDataset, *, tenant_id: str
    ) -> KnowledgeDataset:
        """Create the row; the returned dataset carries the stamped tenant."""
        ...

    async def get_dataset(self, dataset_id: str, *, tenant_id: str) -> KnowledgeDataset | None: ...

    async def list_datasets(self, *, tenant_id: str) -> list[KnowledgeDataset]: ...

    async def update_dataset(
        self, dataset: KnowledgeDataset, *, tenant_id: str
    ) -> KnowledgeDataset | None:
        """Replace the mutable fields (name, description) of the existing
        row — `embedding_config`/`chunk_config` are IMMUTABLE (dimension
        pinned at create; the route 422s any config change before this)."""

    async def delete_dataset(self, dataset_id: str, *, tenant_id: str) -> bool:
        """False when an agent version snapshot references the dataset
        (caller maps to 409 — the eval delete-with-runs precedent)."""
        ...

    async def count_dataset_references(self, dataset_id: str) -> int:
        """How many agent_version snapshots carry the dataset id in
        `snapshot #> '{knowledge,datasets}'` — the 409's receipt (and the
        stale-binding lint's data)."""
        ...

    async def create_document(
        self, document: KnowledgeDocument, chunks: list[KnowledgeChunk], *, tenant_id: str
    ) -> KnowledgeDocument:
        """Persist the document row and its chunks in ONE transaction
        (D65: synchronous ingest is all-or-nothing — a ready document has
        all its chunks). The chunks must carry the document's ids."""

    async def list_documents(
        self, dataset_id: str, *, tenant_id: str
    ) -> list[KnowledgeDocument]: ...

    async def get_document(
        self, document_id: str, dataset_id: str, *, tenant_id: str
    ) -> KnowledgeDocument | None: ...

    async def delete_document(self, document_id: str, dataset_id: str, *, tenant_id: str) -> bool:
        """Cascades the document's chunks (migration 0014 FK ON DELETE)."""
        ...

    async def search_chunks(
        self,
        dataset_ids: list[str],
        embedding: list[float],
        limit: int,
    ) -> list[KnowledgeChunk]:
        """Vector top-k (D68): cosine `ORDER BY embedding <=> :q LIMIT k`
        over the GIVEN dataset ids (tenant-validated by the caller), best
        first. Search-only projections ride each hit — `document_name`
        (joined from the owning document, for the rendered context) and
        `score` (cosine similarity in [0, 1], the D68 min_score input).
        The query vector's dimension must match the datasets' pinned
        config — the service validates before calling; the SQL refuses a
        mismatch honestly (pgvector dimension error)."""


class EvalRepo(Protocol):
    """Evaluation datasets, runs, and results (S11, ADR 0017 §3, D48) —
    tenant-scoped via explicit `tenant_id` kwargs (the McpServerRepo
    pattern; D29: foreign rows read as absent, never leaked)."""

    async def create_dataset(
        self, dataset: EvalDataset, *, tenant_id: str | None = None
    ) -> EvalDataset:
        """Create the row; the returned dataset carries the stamped tenant."""
        ...

    async def list_datasets(self, *, tenant_id: str | None = None) -> list[EvalDataset]: ...

    async def get_dataset(
        self, dataset_id: str, *, tenant_id: str | None = None
    ) -> EvalDataset | None: ...

    async def update_dataset(
        self, dataset: EvalDataset, *, tenant_id: str | None = None
    ) -> EvalDataset | None:
        """Replace the mutable fields (name, description, cases, scorers,
        judge_model) of the existing row; None when absent."""
        ...

    async def delete_dataset(self, dataset_id: str, *, tenant_id: str | None = None) -> bool:
        """False when eval runs reference the dataset (caller maps to 409)
        — the same delete-with-history refusal as AgentRepo.delete."""
        ...

    async def create_run(
        self,
        dataset: EvalDataset,
        agent_id: str,
        agent_version_id: str,
        children: list[tuple[EvalCase, str]],
        *,
        run_id: str | None = None,
        tenant_id: str | None = None,
    ) -> EvalRun:
        """Persist the eval_run row (with the dataset snapshot, D1) plus one
        eval_result per (case, child run_id) — ONE transaction. The caller
        has already enqueued every child run (each run_id was generated
        inside `queue_message` before enqueue, so results can reference the
        runs eagerly, D48). `run_id` lets the caller pin the eval-run id it
        already stamped into the children's metadata; None = repo-generated."""
        ...

    async def list_runs(
        self,
        *,
        agent_id: str | None = None,
        dataset_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[EvalRun]: ...

    async def get_run(self, run_id: str, *, tenant_id: str | None = None) -> EvalRun | None: ...

    async def get_results(
        self, eval_run_id: str, *, tenant_id: str | None = None
    ) -> list[EvalResult]:
        """The child results (case_id, run_id, scores when scored)."""
        ...

    async def save_scores(
        self,
        eval_run_id: str,
        case_id: str,
        scores: list[Score],
        error: str | None,
        *,
        tenant_id: str | None = None,
    ) -> None:
        """Persist-once scoring (D49): sets scores + scored_at on the one
        result row; later calls must not overwrite a scored result."""
        ...

    async def list_version_scores(
        self, agent_id: str, *, tenant_id: str | None = None
    ) -> list[tuple[EvalRun, list[EvalResult]]]:
        """Every eval run of an agent with its results — the version-
        comparison source (grouped by agent_version_id at the API layer)."""
        ...


__all__ = [
    "AgentRepo",
    "ConversationRepo",
    "EvalRepo",
    "ExecutionRepo",
    "KnowledgeRepo",
    "McpServerRepo",
    "ScratchpadRepo",
    "TriggerRepo",
    "WorkflowRepo",
]
