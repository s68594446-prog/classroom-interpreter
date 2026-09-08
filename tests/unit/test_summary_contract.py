from __future__ import annotations

import pytest

from classroom_interpreter.providers.summary_contract import (
    REQUIRED_SECTIONS,
    validate_summary_document,
)
from classroom_interpreter.providers.errors import ProviderFailure
from classroom_interpreter.providers.mocks import MockSummaryProvider


def valid_document() -> str:
    parts = ["# 课程名称：Risk Management", "", "日期：2026-09-08", "", "课程时长：2 小时 30 分钟", ""]
    for index, title in enumerate(REQUIRED_SECTIONS):
        parts.append(f"## {index + 1}. {title}")
        parts.append(f"- 第 {index + 1} 节内容：老师强调了 95% 置信区间与 Black-Scholes 公式。")
        parts.append("")
    return "\n".join(parts)


def test_valid_document_passes():
    assert validate_summary_document(valid_document()) == valid_document().strip()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.replace("## 3. 重要定义", "## 3. 错误标题"),
        lambda d: d.replace("## 4. 重要公式 / 数字 / 条件\n- 第 4 节内容", "## 4. 重要公式 / 数字 / 条件"),
        lambda d: d.replace("# 课程名称：Risk Management", ""),
        lambda d: d.replace("日期：2026-09-08", ""),
        lambda d: d.replace("课程时长：2 小时 30 分钟", ""),
        lambda d: d.replace("## 10. 本节课完整结构化总结\n- 第 10 节内容", "## 10. 本节课完整结构化总结"),
        lambda d: d[: len(d) // 2],  # truncated output
    ],
)
def test_invalid_documents_are_rejected(mutate):
    with pytest.raises(ProviderFailure):
        validate_summary_document(mutate(valid_document()))


def test_extra_or_missing_sections_rejected():
    doc = valid_document().replace(
        "## 2. 核心知识点", "## 2. 核心知识点2"
    )
    with pytest.raises(ProviderFailure):
        validate_summary_document(doc)
    doc = "\n".join(
        line for line in valid_document().splitlines() if not line.startswith("## 7.")
    )
    with pytest.raises(ProviderFailure):
        validate_summary_document(doc)


def test_empty_output_rejected():
    with pytest.raises(ProviderFailure):
        validate_summary_document("   ")


def test_mock_provider_produces_valid_document():
    provider = MockSummaryProvider()
    import asyncio

    document = asyncio.run(
        provider.synthesize(
            course_name="测试课程",
            date="2026-09-08",
            duration_text="1 小时 0 分钟",
            chunk_summaries=["要点 A", "要点 B"],
        )
    )
    assert validate_summary_document(document)
    assert "测试课程" in document
