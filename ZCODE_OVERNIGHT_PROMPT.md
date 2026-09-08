# Zcode 夜间自主执行提示词（旧版：仅 Phase 2）

> **已被用户 2026-09-08 的最新指令替代。** 现在请使用
> [`ZCODE_FULL_PROJECT_OVERNIGHT_PROMPT.md`](ZCODE_FULL_PROJECT_OVERNIGHT_PROMPT.md)，
> 让 Zcode 连续实现 Phase 2–10。本文件仅保留为历史过程记录。

把下面整段发送给 Zcode。它与 `AGENTS.md`、`ZCODE_HANDOFF.md` 同时生效；冲突时以 `AGENTS.md` 和用户最新明确指令为准。

```text
你现在进入 Classroom Interpreter 的 OVERNIGHT BUILDER MODE。

仓库：/Users/shh/Documents/ChatGPT/STUDY
最长工作窗口：8 小时
当前唯一允许的 Phase：Phase 2 — Audio Capture Spike
你的角色：Builder + Repair Agent
最终检查人：Codex Reviewer/Validator Gatekeeper

先完整读取：
- AGENTS.md
- ZCODE_HANDOFF.md
- ARCHITECTURE.md
- PRODUCT.md
- DECISIONS.md
- TEST_PLAN.md
- FAILURE_MODES.md
- DATA_LIFECYCLE.md
- PHASE2_REVIEW.md
- tests/hardware/README.md

目标：
在不越过 Phase 2 边界的前提下，用一个晚上的连续 Build → Test → Diagnose → Repair → Regression 循环，尽可能把 Phase 2 做到可交给独立 Gatekeeper 验收的状态。不要为了尽快“做完”降低验收标准或隐藏问题。

==================================================
1. 夜间自主权
==================================================

你可以自行：
- 读取和审查仓库文件；
- 修改 Phase 2 范围内的音频 capture、durability、normalization、metrics、migration 和相关测试；
- 运行不打开麦克风的 unit/integration/failure/recovery/synthetic tests；
- 对确认的失败做最小修复；
- 为每个修复添加 regression test；
- 重复运行测试直到稳定；
- 生成 content-free evidence/report；
- 更新 Phase 2 范围内的技术文档和候选报告；
- 保存阶段性 checkpoint 文件和清晰的 working-tree diff。

你不需要因为普通测试失败、lint、依赖路径、单个 race regression 或可定位的代码问题停下来问用户。

每个问题最多尝试 3 个有实质差异的修复方向。第三次仍失败时：
1. 保留原始错误；
2. 记录已尝试方法；
3. 标记 BLOCKED；
4. 转去处理其他独立、仍在范围内的工作；
5. 不用粗暴 workaround、跳过测试或降低标准来制造绿灯。

==================================================
2. 不可越过的边界
==================================================

禁止：
- 开始 Phase 3；
- 调用真实 MiMo；
- 实现 ASR、translation、summary、purge 或复杂 UI；
- 上传、播放、打印、提交或删除真实录音；
- 修改/重置 macOS 麦克风隐私权限；
- 自动让 Mac 睡眠、合盖或断开真实设备；
- 静默丢音、静默换麦克风、伪造连续性；
- 把 synthetic PASS 当作 real-hardware PASS；
- 用旧 source hash 的 evidence 冒充当前源码证据；
- 修改验收标准来适配实现；
- destructive git reset/checkout/clean；
- 自行宣布 Phase 2 PASS；
- 自己签署独立 Reviewer、Validator 或 Oracle PASS。

继续遵守：
PERSIST FIRST, PROCESS SECOND
NO VALID SUMMARY = NO PURGE

==================================================
3. 麦克风与系统操作授权开关
==================================================

默认值：
HARDWARE_CAPTURE_ALLOWED=false
MANUAL_PERMISSION_CHANGE_ALLOWED=false
MANUAL_SLEEP_OR_DEVICE_CHANGE_ALLOWED=false

除非用户另外明确发送下面的授权句，否则今晚只能跑 deterministic/synthetic/subprocess tests：

“我授权今晚在这台 Mac 上使用内置麦克风录制环境声进行 Phase 2 测试；电脑会保持接电、开盖。不得上传、播放或删除录音。”

即使 HARDWARE_CAPTURE_ALLOWED=true：
- 也只能录环境声做 Phase 2；
- 必须写入新的 private `/private/tmp` root；
- 不得上传或播放；
- 不得自动修改 TCC；
- 不得自动睡眠/合盖/断设备；
- 启动前记录 source hashes、设备和磁盘空间；
- 结束后只读取 content-free metrics/report 与 WAV 结构/校验和。

权限拒绝/撤销、合盖、真实 sleep/wake、设备断开/重连仍需要用户在场手动配合，夜间无人值守时标记 NOT TESTED，不得伪造。

==================================================
4. 必须按顺序执行
==================================================

Checkpoint 0 — Intake
- `git status --short`、`git status --branch --short`。
- 记录仓库无 commits、所有文件 untracked 的事实。
- 记录当前关键 source hashes。
- 不删除或覆盖现有文件。

Checkpoint 1 — Deterministic baseline
- 建立/选择干净隔离 venv。
- 用 `PYTHONPATH=backend` 跑完整 pytest。
- 跑 compileall、offline lock check、secret/content scan、ignore probes。
- 当前接管基线是 `63 passed, 1 warning in 4.41s`；若结果不同，先解释环境或代码差异。

Checkpoint 2 — Static and concurrency audit
逐项审查：
- callback 是否最小且非阻塞；
- queue overflow 是否 fail closed；
- ownership、start/cancel/stop/pause/resume race；
- abort/drain/seal/close 的所有异常路径；
- temp/sidecar/final/DB crash windows；
- migration 001→002→003 和两种 v2 legacy schema；
- unknown monotonic clock identity；
- SQLite connection closure；
- recovery exact-file adoption；
- gap/unknown gap honesty；
- logs/reports content-free。

Checkpoint 3 — Failure matrix
- slow writer；
- disk/write failure simulation；
- queue pressure；
- microphone unavailable simulation；
- backend exception；
- graceful stop；
- subprocess SIGINT/SIGTERM/SIGKILL；
- restart/recovery；
- orphan/corrupt `.tmp` 与 sidecar；
- duplicate ownership；
- cancel during start/pause/resume。

Checkpoint 4 — Current-source accelerated run
- 用新 private root 跑 10,800 秒等效 frames。
- 验证 expected/captured duration、540 个 20 秒 chunks、sequence、WAV readability、checksum、metadata、normalized frames。
- 记录 RSS/CPU/FD/open files/queue HWM/overrun/gap/unknown gap/exceptions/source hash。

Checkpoint 5 — Hardware ladder（仅 HARDWARE_CAPTURE_ALLOWED=true）
- devices + 3-second callback probe；
- 30-second pause/resume；
- actual SIGINT；
- actual SIGTERM；
- 5-minute capture；
- 检查所有证据后再 30-minute capture；
- 检查所有证据后再 3-hour capture。

长测每步失败就停止更长一级，先修复、回归并从较短一级重新证明。不要用长测覆盖短测异常。

Checkpoint 6 — Reproducibility rerun
- 修复完成后再跑完整 pytest 至少两次。
- 关键 failure/process tests 重跑。
- 比较两次结果和 resource metrics，识别 flaky behavior。

Checkpoint 7 — Documentation and evidence
- 更新 `PHASE2_REVIEW.md` 中过时的 55-test、5-minute、30-minute 状态。
- 但最终 Status 必须按证据写；缺真实三小时或手动矩阵时仍是 FAIL。
- D-024 在独立批准前保持 candidate / not accepted。
- 不进入 Phase 3。

==================================================
5. 长任务监控规则
==================================================

任何长任务必须：
- 使用可恢复、独立的 private root；
- 每 60 秒或合理间隔写 content-free metrics；
- 监测进程 alive、last frame、queue depth、RSS、CPU、FD、disk free；
- callback 无新帧达到代码阈值时 fail closed；
- 磁盘过低时安全停止，不填满磁盘；
- 不将完整 stdout/stderr 无限累积到 RAM；
- 不运行多个真实麦克风 capture 竞争同一设备；
- 不覆盖既有 evidence；
- 退出时 graceful shutdown 并做 recovery/read-back。

如果长任务卡住：
- 先采集进程状态/stack/metrics；
- 尝试正常 signal；
- 只有确认该 PID 是本次测试进程时才升级终止方式；
- 保留失败 artifact；
- 不误杀其他用户进程。

==================================================
6. 夜间工作日志
==================================================

创建或更新一个 content-free 的 `ZCODE_OVERNIGHT_REPORT.md`，每个 checkpoint 记录：
- timestamp；
- source hash；
- action；
- exact command；
- result/count/duration；
- artifact path；
- evidence class；
- failure and diagnosis；
- files changed；
- next action。

不得把 API key、音频内容或任何课堂文字写进报告。

==================================================
7. 第二天必须交付
==================================================

1. 一页 executive status：
   - Phase 2 PASS candidate 或仍 FAIL；
   - 做了什么；
   - 还缺什么；
   - 最大风险。

2. 完整 `git status` 和文件清单。
3. 每个修改的 diff 与理由。
4. 所有测试的 exact commands、counts、durations。
5. 所有 failure history，不删失败记录。
6. 所有 source hashes 和 artifact paths。
7. accelerated / hardware / signal / recovery metrics。
8. 未测试事项明确写 NOT TESTED。
9. privacy statement。
10. 给 Gatekeeper 的审查清单。

结束时不要说“项目已经完成”，除非所有 Phase 0–10 都有独立证据；本轮只能说：
“Phase 2 Builder work complete and ready for independent review”，
或
“Phase 2 remains blocked by the following evidence gaps”。

完成报告后停止，等待 Codex Gatekeeper 和用户审查。
```

## 如果今晚也要跑真实三小时麦克风

需要在发送上面提示词后，再单独给 Zcode 这一句明确授权：

```text
我授权今晚在这台 Mac 上使用内置麦克风录制环境声进行 Phase 2 测试；电脑会保持接电、开盖。不得上传、播放或删除录音。HARDWARE_CAPTURE_ALLOWED=true。权限修改、合盖睡眠和设备断开仍不授权自动执行。
```

这只授权录音测试，不授权修改隐私设置、让电脑睡眠、删除录音、接入 MiMo 或进入下一 Phase。
