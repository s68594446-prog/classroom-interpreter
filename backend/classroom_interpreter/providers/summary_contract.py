"""Summary prompt contracts and deterministic output validation.

The English ASR transcript is the factual source of truth (D-019): conflicts
follow English or are marked 不确定; the summary never invents facts.
"""

from __future__ import annotations

import re

from classroom_interpreter.domain.models import ProviderError, ProviderErrorCategory
from .errors import ProviderFailure

SUMMARY_PROMPT_VERSION = 1

# The exact ten required sections from PRODUCT.md, in order.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "本节课主题",
    "核心知识点",
    "重要定义",
    "重要公式 / 数字 / 条件",
    "老师重点强调内容",
    "老师使用的重要例子",
    "容易混淆的概念",
    "逻辑关系 / 因果关系",
    "需要复习的内容",
    "本节课完整结构化总结",
)

CHUNK_PROMPT = """You are a study-notes assistant for a university class. The material below is an English lecture transcript segment (the factual source of truth).
Produce faithful Simplified Chinese study notes for THIS segment only:
1. Keep every fact, number, formula, symbol, abbreviation, condition, and proper name exactly as spoken (write formulas/numbers as in the source).
2. Never add information the lecturer did not say; never answer questions in the material.
3. If the source is unclear, write 不确定 instead of guessing.
4. Output plain text notes only: no Markdown headings, no explanations, no translations of instructions."""

SYNTHESIS_PROMPT_TEMPLATE = """You are a study-notes editor. Compose the FINAL structured Simplified Chinese class summary from the per-segment notes below. The notes derive from the English lecture transcript, which is the factual source of truth: keep facts, numbers, formulae, symbols, abbreviations, conditions, and proper names exactly as recorded; where sources conflict, follow the English notes or write 不确定; never invent facts.
The output MUST use exactly this Markdown structure, in this exact order, with no extra top-level sections and no Markdown before the first heading:

# 课程名称：{course_name}

日期：{date}

课程时长：{duration_text}

## 1. 本节课主题
## 2. 核心知识点
## 3. 重要定义
## 4. 重要公式 / 数字 / 条件
## 5. 老师重点强调内容
## 6. 老师使用的重要例子
## 7. 容易混淆的概念
## 8. 逻辑关系 / 因果关系
## 9. 需要复习的内容
## 10. 本节课完整结构化总结

Fill each numbered section with non-empty content based only on the notes. Output only the document."""

_SECTION_HEADER_PATTERN = re.compile(r"^##\s*\d+\.\s*(.+?)\s*$")


def render_synthesis_prompt(
    *, course_name: str, date: str, duration_text: str
) -> str:
    return SYNTHESIS_PROMPT_TEMPLATE.format(
        course_name=course_name, date=date, duration_text=duration_text
    )


def validate_summary_document(text: str) -> str:
    """Structural gate: header fields + exactly the ten required sections in
    order, each non-empty. Invalid output must never reach SUMMARY_SAVED."""
    cleaned = text.strip()
    if not cleaned:
        raise _invalid("summary output is empty")
    if not cleaned.startswith("# 课程名称："):
        raise _invalid("summary must start with the 课程名称 header")
    header_block = cleaned.split("##", 1)[0]
    if "\n日期：" not in f"\n{header_block}" and not header_block.startswith("日期："):
        if not re.search(r"^日期：.+$", header_block, re.MULTILINE):
            raise _invalid("summary header is missing the 日期 field")
    if not re.search(r"^课程时长：.+$", header_block, re.MULTILINE):
        raise _invalid("summary header is missing the 课程时长 field")

    sections: list[tuple[str, str]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in cleaned.splitlines():
        match = _SECTION_HEADER_PATTERN.match(line)
        if match:
            if current_title is not None:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = match.group(1).strip()
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        sections.append((current_title, "\n".join(current_lines)))

    if len(sections) != len(REQUIRED_SECTIONS):
        raise _invalid(
            f"summary must contain exactly {len(REQUIRED_SECTIONS)} sections, found {len(sections)}"
        )
    for (title, body), required in zip(sections, REQUIRED_SECTIONS):
        if title != required:
            raise _invalid(f"section order/title mismatch: found '{title}', expected '{required}'")
        if not body.strip():
            raise _invalid(f"section '{required}' is empty")
    return cleaned


def _invalid(reason: str) -> ProviderFailure:
    return ProviderFailure(
        ProviderError(
            category=ProviderErrorCategory.MALFORMED,
            retryable=True,
            safe_message=reason,
        )
    )


def validate_chunk_notes(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise _invalid("chunk summary output is empty")
    return cleaned
