"""SpanSink port (S7, ADR 0021 §1, D71) — the one seam spans cross.

The runtime builds `SpanRecord`s and flushes buffered batches to a
`SpanSink`. The runtime imports NO opentelemetry anything: Postgres
persistence (`SqlSpanRepo`) and the OTLP exporter (`OtelSpanSink`) are
both ordinary adapters of this port, and the container fans one run's
records out to both. Sink failures must NEVER touch a run's outcome —
implementations report their own trouble to the log (the D5 stance
transposed to observability).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from jarvis.domain.spans import SpanRecord

logger = logging.getLogger("jarvis.spans")


@runtime_checkable
class SpanSink(Protocol):
    async def export(self, spans: Sequence[SpanRecord]) -> None:
        """Persist/forward one batch of finished spans. Batched by
        contract — the runtime flushes at iteration boundaries, at a
        pause, and at the terminal (D74), not one call per span."""
        ...


class FanoutSpanSink:
    """Tee a batch to several sinks, isolating failures: one broken sink
    logs and never blocks the others (and never the run)."""

    def __init__(self, *sinks: SpanSink) -> None:
        self._sinks = sinks

    async def export(self, spans: Sequence[SpanRecord]) -> None:
        if not spans:
            return
        for sink in self._sinks:
            try:
                await sink.export(spans)
            except Exception:  # noqa: BLE001 — observability never breaks a run (D5)
                logger.warning("span sink %r failed; batch dropped", sink, exc_info=True)


__all__ = ["FanoutSpanSink", "SpanSink"]
