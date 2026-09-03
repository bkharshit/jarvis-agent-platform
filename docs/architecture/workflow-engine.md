# Workflow Engine (future phase — stub)

Anticipated seam: a **sibling executor** to `AgentRuntime`, reusing the same
event model (`ExecutionEvent` + terminal semantics), persistence
(`agent_executions`-style rows with step executions), and limits layer. Nodes
are versioned classes; an agent node invokes `AgentRuntime.run()` like any
other caller. Persistence/observability/limits compose as engine layers
(Dify/graphon pattern). Not started.