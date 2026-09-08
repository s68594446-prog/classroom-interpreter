# Classroom Interpreter

macOS 本地优先的课堂实时英译中与课后总结工具。

## Current status

`Phase 10 — Final Audit Candidate（overnight build；未验收）`

完整实现候选：音频采集、语义分段、MiMo ASR/翻译/总结 adapter、持久化队列、实时 UI、总结原子保存与验证、exact-manifest 清理、加速 soak（1,800 segments）。真实 MiMo、真实三小时麦克风 soak、权限/睡眠/设备矩阵仍未测试，等待独立 Reviewer/Validator/Oracle 与用户验收。见 `reports/FINAL_BUILDER_HANDOFF.md`。

请先审阅：

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [PRODUCT.md](PRODUCT.md)
- [AGENTS.md](AGENTS.md)
- [DATA_LIFECYCLE.md](DATA_LIFECYCLE.md)
- [FAILURE_MODES.md](FAILURE_MODES.md)
- [TEST_PLAN.md](TEST_PLAN.md)
- [DECISIONS.md](DECISIONS.md)

## V1 boundary

V1 仅支持 Mac 麦克风、English → Simplified Chinese、MiMo 云端 ASR/文本模型、实时字幕、课后结构化总结、故障恢复，以及总结验证后的临时数据清理。

这是 local-first application with cloud AI processing，不是 `100% local`。课堂音频片段与文字会按处理阶段发送给 MiMo。

## Safety invariants

```text
PERSIST FIRST, PROCESS SECOND
NO VALID SUMMARY = NO PURGE
```

V1 的删除是逻辑文件删除，不保证 SSD/APFS 上的法证级安全擦除。

## Phase 1 commands

```bash
uv sync --extra dev
.venv/bin/python -m pytest
```

```bash
cd frontend
npm install
npm run typecheck
npm run build
```

本地 backend：

```bash
CLASSROOM_INTERPRETER_DATA_DIR=/private/tmp/classroom-interpreter-dev \
  uv run uvicorn classroom_interpreter.main:app --host 127.0.0.1 --port 8000
```

`/health` 会把 microphone、ASR、translation、queue 和 network 明确标为 `PHASE_1_NOT_IMPLEMENTED`，避免把骨架误报为产品可用。

## Next gate

Phase 1 Review 获得确认后，才进入 `Phase 2 — Audio Capture Spike`。Phase 2 必须先验证 Python/CoreAudio-compatible 路线，再决定是否升级为 Swift/AVFoundation helper。
# Phase 2 audio capture spike (not accepted)

The Phase 1 shell remains the default web application. Phase 2 is an explicit,
backend-owned audio CLI; launching the web shell does not open the microphone.
No MiMo, translation, summary, semantic segmentation, or deletion is added.

```
uv sync --extra dev --extra audio
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio devices
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --seconds 300
PYTHONPATH=backend uv run --extra dev --extra audio pytest
```

The explicit `PYTHONPATH=backend` is required on the currently observed macOS
development environment: editable-install `.pth` files have the hidden file flag
and the plain installed module launch did not resolve. This is a known packaging
issue, not proof that the installed application is ready.

See `tests/hardware/README.md` for opt-in hardware tests and privacy limitations,
`DECISIONS.md` D-024 for the candidate design, and `PHASE2_REVIEW.md` for evidence
and blockers. Three-hour hardware testing is deferred at the user's request.
