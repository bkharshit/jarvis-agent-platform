"""Custom persistence types (S8, ADR 0020 §1).

`VectorType` wraps pgvector's `vector` WITHOUT the pgvector python
dependency (~30-line UserDefinedType): binds render the text form
`'[v1,v2,…]'`, results parse back to `list[float]`. The column type is
dimension-free on purpose — the dimension is enforced at WRITE time from
the dataset's pinned `EmbeddingConfig`, never by the column (ADR 0020
§1). Queries pass the query vector the same way, with an explicit
`CAST(:q AS vector)` beside the `<=>` operator.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.types import UserDefinedType


class VectorType(UserDefinedType[list[float]]):
    """pgvector `vector` as SQLAlchemy UserDefinedType (no new dep)."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "VECTOR"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: Any) -> Any:
            if value is None:
                return None
            return render_vector(value)

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: Any) -> list[float] | None:
            if value is None:
                return None
            return parse_vector(value)

        return process


def render_vector(value: Any) -> str:
    """Any float sequence → pgvector's text form `'[1.0,2.0]'`."""
    return "[" + ",".join(repr(float(v)) for v in value) + "]"


def parse_vector(value: Any) -> list[float] | None:
    """pgvector text form → floats. The driver returns the `'[1,2]'`
    string under asyncpg (no native codec registered)."""
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if not isinstance(value, str):
        raise TypeError(f"cannot parse pgvector value: {value!r}")
    inner = value.strip().strip("[]")
    if not inner:
        return []
    return [float(part) for part in inner.split(",")]


__all__ = ["VectorType", "parse_vector", "render_vector"]
