from __future__ import annotations

import math
from dataclasses import dataclass

from classroom_interpreter.domain.models import ContextSegment, ContextWindow
from classroom_interpreter.storage import Database


def estimate_tokens(text: str) -> int:
    """Deterministic CJK-aware estimator: one token per CJK character and one
    per four remaining characters (conservative upper bound)."""
    if not text:
        return 0
    cjk = sum(1 for char in text if ord(char) >= 0x2E80)
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)


@dataclass(slots=True)
class ContextAgent:
    """Builds the bounded prior context for one translation request.

    Reads only committed DONE predecessors of the same session. The window is
    bounded three ways (count, wall-clock seconds, estimated tokens) per
    D-029; a smaller valid window is returned when limits bind.
    """

    database: Database
    max_segments: int = 8
    max_seconds: int = 90
    max_tokens: int = 1200

    def build(self, session_id: str, segment_sequence: int) -> ContextWindow:
        with self.database.connect() as connection:
            current = connection.execute(
                "SELECT english_text FROM asr_results WHERE session_id = ? AND segment_sequence = ?",
                (session_id, segment_sequence),
            ).fetchone()
            if current is None:
                raise KeyError(f"no committed ASR result for {session_id}:{segment_sequence}")
            rows = connection.execute(
                """
                SELECT s.sequence, s.start_ms, s.end_ms, a.english_text, t.chinese_text
                FROM segments s
                JOIN asr_results a ON a.segment_id = s.id
                JOIN translation_results t ON t.segment_id = s.id
                WHERE s.session_id = ? AND s.sequence < ? AND s.state = 'DONE'
                ORDER BY s.sequence DESC
                """,
                (session_id, segment_sequence),
            ).fetchall()
            current_segment_id = connection.execute(
                "SELECT id FROM segments WHERE session_id = ? AND sequence = ?",
                (session_id, segment_sequence),
            ).fetchone()["id"]

        selected: list[ContextSegment] = []
        token_budget = self.max_tokens
        window_start_ms = 0
        window_end_ms = 0
        for row in rows:  # newest → oldest
            if len(selected) >= self.max_segments:
                break
            span_ms = window_end_ms - row["start_ms"] if selected else row["end_ms"] - row["start_ms"]
            if selected and span_ms > self.max_seconds * 1000:
                break
            tokens = estimate_tokens(row["english_text"]) + estimate_tokens(row["chinese_text"])
            if selected and tokens > token_budget:
                break
            selected.append(
                ContextSegment(
                    sequence=row["sequence"],
                    english_text=row["english_text"],
                    chinese_text=row["chinese_text"],
                )
            )
            token_budget -= tokens
            window_end_ms = max(window_end_ms, row["end_ms"])
            window_start_ms = row["start_ms"]
        selected.reverse()  # oldest → newest for chronological rendering
        estimated = sum(
            estimate_tokens(s.english_text) + estimate_tokens(s.chinese_text)
            for s in selected
        )
        return ContextWindow(
            session_id=session_id,
            current_segment_id=current_segment_id,
            previous_segments=tuple(selected),
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            estimated_tokens=estimated,
            current_english_text=current["english_text"],
        )
