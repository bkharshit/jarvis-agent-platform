"""Paragraph-aware chunking (S8, ADR 0020 §3, D65).

Pure text → list[str], no IO: chunks pack to `chunk_chars` with an
`overlap_chars` tail carry. Paragraph boundaries are respected whenever
one fits — a paragraph longer than a chunk is hard-split. Deterministic:
the same text + config always yields the same chunks."""

from __future__ import annotations

from jarvis.domain.knowledge import ChunkConfig


def chunk_text(text: str, config: ChunkConfig) -> list[str]:
    """Pack paragraphs into `chunk_chars` windows; when a window closes,
    its tail (up to `overlap_chars`, cut at a word boundary) opens the
    next one so adjacent chunks share context."""
    if not text.strip():
        return []
    if len(text) <= config.chunk_chars:
        return [text.strip()]

    # Paragraphs (blank-line separated); one longer than a chunk is
    # hard-split into pieces, each at most chunk_chars.
    blocks: list[str] = []
    for paragraph in (p.strip() for p in text.split("\n\n") if p.strip()):
        blocks.extend(_hard_split(paragraph, config.chunk_chars))

    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= config.chunk_chars:
            current = candidate
            continue
        chunks.append(current)
        carry = _carry(current, config.overlap_chars)
        candidate = f"{carry}\n\n{block}" if carry else block
        # The carry crowded the block past the window — drop the carry.
        current = candidate if len(candidate) <= config.chunk_chars else block
    if current:
        chunks.append(current)
    return chunks


def _carry(text: str, overlap_chars: int) -> str:
    """The chunk's tail, up to `overlap_chars`, cut at a word boundary."""
    if overlap_chars <= 0:
        return ""
    tail = text[-overlap_chars:]
    space = tail.find(" ")
    if space >= 0:
        tail = tail[space + 1 :]
    return tail.strip()


def _hard_split(text: str, chunk_chars: int) -> list[str]:
    if len(text) <= chunk_chars:
        return [text]
    return [
        piece
        for piece in (text[i : i + chunk_chars].strip() for i in range(0, len(text), chunk_chars))
        if piece
    ]


__all__ = ["chunk_text"]
