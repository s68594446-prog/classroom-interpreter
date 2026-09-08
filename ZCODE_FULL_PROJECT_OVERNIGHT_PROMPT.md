# Zcode 全项目夜间连续开发提示词

> 这是用户在 2026-09-08 最新授权的执行提示词，替代此前“夜间只做 Phase 2”的安排。  
> 完整背景、现有代码、历史证据和安全约束仍以 `ZCODE_HANDOFF.md` 为准。

把下面整段发送给 Zcode：

```text
你现在进入 Classroom Interpreter 的 FULL PROJECT OVERNIGHT BUILD MODE。

仓库：/Users/shh/Documents/ChatGPT/STUDY
建议连续工作窗口：一个晚上，约 8–12 小时
目标：在现有 Phase 0/1/2 基础上，按顺序完成 Phase 2–10 的完整可运行候选版本。

用户已明确授权：
“让 Zcode 直接做完整个项目，有问题后面再修。”

因此你不需要在每个 Phase 完成后等待人工批准，可以连续进入下一 Phase。
但这只是 implementation scheduling exception，不代表任何 Phase 自动验收通过。

角色：
- Zcode = Builder + Repair Agent
- Codex = build 完成后的独立 Reviewer / Validator Gatekeeper
- 用户 = 产品所有者和最终批准人

==================================================
0. 开始前必须完整读取
==================================================

1. AGENTS.md
2. ZCODE_HANDOFF.md
3. ZCODE_FULL_PROJECT_OVERNIGHT_PROMPT.md
4. ARCHITECTURE.md
5. PRODUCT.md
6. DATA_LIFECYCLE.md
7. FAILURE_MODES.md
8. TEST_PLAN.md
9. DECISIONS.md
10. PHASE2_REVIEW.md
11. tests/hardware/README.md

先执行只读接管：
- git status --short
- git status --branch --short
- 文件与测试清单
- 当前源码 hashes
- 当前确定性测试

仓库目前没有 commit，main 上全部文件均为 untracked。不得 destructive reset、
checkout 或 clean，不得假设存在可恢复 baseline。

==================================================
1. 最终产品必须实现的用户旅程
==================================================

1. 用户打开本地应用。
2. 应用显示 microphone、storage、MiMo、queue 和 recovery readiness。
3. 用户输入 course name，点击 Start Class。
4. backend 持续拥有并采集 Mac 麦克风，浏览器刷新不影响 capture。
5. 音频先持久化，再进入 semantic segmentation 和 MiMo ASR。
6. UI 先显示 current English，再按顺序提交 English + Simplified Chinese。
7. 网络/MiMo 失败时已录音频不丢失，queue 可恢复，延迟可见。
8. Pause/Resume 产生明确 intentional gap，不生成假静音。
9. End Class 停止新录音并 drain 全部已持久化任务。
10. Summary 根据 English transcript 作为事实源生成结构化中文总结。
11. Summary 原子写入、flush/sync、读回、结构校验、hash 验证。
12. 只有 summary 验证成功后，Cleanup 才按 exact manifest 清理本节临时数据。
13. 最终只保留 Final Summary 和严格白名单的 content-free purge metadata。
14. 崩溃后重新打开，用户看到 Resume Recovery 或明确 Delete Session，不静默删除。

==================================================
2. 永远不能破坏的原则
==================================================

PERSIST FIRST, PROCESS SECOND
NO VALID SUMMARY = NO PURGE

- backend owns microphone；browser 只控制/显示。
- capture 不得等待 network/provider。
- Provider call 不得发生在 SQLite write transaction 中。
- durable DB rows 是 queue source of truth；内存 queue 只作 wake-up hint。
- delivery 是 at-least-once；committed result 必须 idempotent、唯一、有序。
- callback 不做 DB/network/VAD/resampling/heavy logging。
- queue overflow、device loss、sleep、callback stall 不得静默丢音。
- 不得伪造缺失音频、翻译、总结或测试证据。
- ordinary logs、fixtures、screenshots、reports 不得含 key、音频或课堂文字。
- 无 silent provider/model/device fallback。
- context 必须按 time/count/token 有界。
- summary facts/numbers/formulas/terms 以 English ASR 为事实源；冲突跟随英文或标不确定。
- cleanup 只能删除 validated exact session manifest；永不删除 app root 或 summary store。
- no valid summary 时 cleanup 永远 fail closed。
- 不扩展 V1：只有 macOS、English → Simplified Chinese、MiMo、单 active session。

==================================================
3. 连续开发规则
==================================================

必须仍按顺序开发：

Phase 2 Audio
→ Phase 3 ASR
→ Phase 4 Translation
→ Phase 5 Live UI
→ Phase 6 Final Summary
→ Phase 7 Data Purge
→ Phase 8 Reliability
→ Phase 9 Soak
→ Phase 10 Final Audit Candidate

每完成一个 Phase：
1. 运行该 Phase 的确定性测试；
2. 写 `reports/PHASE_<N>_BUILDER_REPORT.md`；
3. 记录文件、命令、计数、失败、未知项和 evidence class；
4. 保存当前 source hashes；
5. 再进入下一 Phase。

允许下一 Phase 继续实现，即使前一 Phase 还有真实外部证据缺口；但报告必须明确：
- IMPLEMENTED / PARTIAL / BLOCKED
- VERIFIED / SIMULATED / NOT TESTED
- 哪些旧 gate 仍然 open

不得把 `implemented` 写成 `accepted`。

==================================================
4. 自主 Build–Test–Repair 循环
==================================================

对每个可定位问题：
1. 保存原始错误和复现命令；
2. 确定影响范围；
3. 做最小修复；
4. 添加 regression test；
5. 跑 targeted tests；
6. 跑完整 tests；
7. 更新 Phase report；
8. 继续下一工作项。

普通失败不要停下来问用户。每个问题最多尝试 3 个实质不同的方案；仍失败则：
- 标记 BLOCKED；
- 保留所有错误和尝试；
- 不降低验收标准；
- 转去完成其他不依赖该 blocker 的工作。

只有以下情况必须停下该项：
- 需要用户提供/确认 API key、付费调用或外部账户；
- 需要录制真实环境声而没有明确授权；
- 需要用户手动修改隐私权限、合盖、睡眠、断开设备；
- 可能删除/覆盖用户数据；
- 需要改变 V1 产品范围或冻结安全原则；
- 无法判断精确删除/写入目标。

这些情况不阻止你继续做其他独立模块。

==================================================
5. Phase 2 — Audio 收口
==================================================

基于现有 sounddevice/PortAudio/CoreAudio + SoXR 候选实现：
- 审查 callback、bounded queue、ownership、race、abort/drain/seal/close。
- 审查 temp/sidecar/rename/fsync/checksum/read-back/DB commit crash windows。
- 审查 migrations 001/002/003 和 legacy monotonic clock handling。
- 跑当前 source 的 unit/integration/failure/process tests。
- 跑当前 source accelerated 10,800 秒等效测试。
- 若 `HARDWARE_CAPTURE_ALLOWED=true`，按 3s → pause → signal → 5m → 30m → 3h 梯级实测。
- 无授权时，硬件/权限/睡眠/重连证据写 NOT TESTED，继续后续实现。

当前已知基线和历史证据必须从 ZCODE_HANDOFF.md 读取；旧 hash 的 30m/accelerated
结果只能作历史证据，不能冒充当前 evidence。

==================================================
6. Phase 3 — Segmentation + MiMo ASR
==================================================

实现：
- 本地 deterministic VAD/silence segmentation；不是把 20 秒 recovery chunk 直接当语义段。
- duration bounds、overlap 和 recoverable segment materialization。
- 每个 segment 在 provider call 前持久化。
- SQLite lease/claim/retry/restart。
- MiMo `ASRProvider` adapter：`POST /v1/chat/completions`、model `mimo-v2.5-asr`、
  `asr_options.language=en`、单个 Base64 WAV/MP3，遵守 payload size gate。
- error classification：AUTH/BALANCE/RATE_LIMIT/TIMEOUT/SERVER/MALFORMED/
  CONTENT_FILTER/NETWORK/UNKNOWN。
- bounded retry/backoff/jitter；non-retryable → RECOVERY_REQUIRED。
- request payload contract tests。
- deterministic ASR mock error matrix。
- provider request 不在 DB transaction 中。
- idempotent one result per segment；稳定 sequence。

只有当本机已有安全配置的 key 且用户授权真实调用时才跑短 real MiMo test。
无 key 时完整实现 adapter 和 mock/contract tests，并把 real MiMo 标 NOT TESTED。
禁止打印、提交或回显 key/audio/base64/classroom response。

==================================================
7. Phase 4 — Translation
==================================================

实现：
- English → Simplified Chinese only。
- 每个 session 内 ordered commit；不是 process-global 串行锁。
- 只读取 committed previous segments。
- context 以时间、条数、估算 token 三重上限约束。
- prompt 只要求忠实翻译当前 segment，不总结、不回答、不添加信息。
- 保留数字、公式、符号、缩写、人名和专业词。
- `english_text_sha256` 绑定 source English。
- timeout/retry/restart/idempotency。
- terminology、number、formula、context truncation fixtures。
- deterministic translation mock error matrix。
- real MiMo short test 仅在安全 key 和用户授权存在时运行，否则 NOT TESTED。

==================================================
8. Phase 5 — Live UI
==================================================

实现最小实用 React/Vite UI：
- localhost only。
- course name；Start/Pause/Resume/End Class。
- session timer、LIVE/PAUSED/FINALIZING/RECOVERY_REQUIRED。
- microphone、storage、network/provider、ASR、translation、queue health。
- current English 与 committed English+Chinese 明确区分。
- committed history 按 sequence 稳定，不回写篡改。
- backlog/live delay 可见。
- WebSocket refresh/reconnect replay from persisted cursor。
- 断线重连不重复 display、不丢 committed item。
- browser refresh 不拥有或终止 microphone。
- recovery screen 和用户可理解的 action。
- 不做装饰动画、主题系统或无关功能。

测试：frontend typecheck/build、backend API/WebSocket integration、replay/order/
duplicate/reconnect/control idempotency tests。

==================================================
9. Phase 6 — Final Summary
==================================================

实现：
- End Class 先停止新 capture，再等待所有 persisted segment 完成。
- 若有 terminal segment failure，不得生成伪完整总结；进入 RECOVERY_REQUIRED。
- hierarchical/resumable summary chunking，bounded context。
- English transcript 是事实源；Chinese translation 只辅助组织。
- required Markdown header：课程名称、日期、课程时长。
- exactly ten required sections：
  1. 本节课主题
  2. 核心知识点
  3. 重要定义
  4. 重要公式 / 数字 / 条件
  5. 老师重点强调内容
  6. 老师使用的重要例子
  7. 容易混淆的概念
  8. 逻辑关系 / 因果关系
  9. 需要复习的内容
  10. 本节课完整结构化总结
- 不明确内容标 `不确定`；不得创造事实。
- invalid/empty/missing-section output 必须失败。
- summary 写到 session 目录之外。
- temp write → flush/fsync → atomic rename → directory sync → read-back →
  structure validation → SHA256 match → SUMMARY_SAVED。
- resumable multi-pass progress；crash 后不重复破坏已验证结果。
- deterministic SummaryProvider mock 与 invalid-output matrix。
- real MiMo summary test 只有在安全 key/授权存在时运行，否则 NOT TESTED。

==================================================
10. Phase 7 — Data Purge
==================================================

只用 synthetic fixtures 测删除；夜间无人值守不得删除任何现有真实录音或用户数据。

实现：
- `NO VALID SUMMARY = NO PURGE` hard gate。
- Cleanup 接收 exact validated session manifest。
- path containment、symlink/escape/root/summary-store protection。
- purge step journal，部分失败可恢复、幂等重试。
- 删除 raw capture、segments、transcripts、translations、queue rows、summary-work、
  debug content、temporary cache。
- summary 永远在 purgeable session tree 外。
- purge 后重新 read-back retained summary 并核对 hash。
- 只保留 `purged_session_metadata` 白名单字段，严禁课堂文字或 summary body。
- 不声明 APFS/SSD forensic secure erase；仅 logical deletion。

必须测试：wrong state、missing/invalid summary、path traversal、symlink、partial purge、
restart、duplicate purge、retained summary mismatch、content-log residue。

==================================================
11. Phase 8 — Reliability
==================================================

建立并自动化 failure matrix：
- Wi-Fi/network loss；
- 429/500/503/timeout/auth/balance/malformed/content-filter；
- provider latency/backlog；
- backend restart；
- browser refresh；
- duplicate event/delivery；
- expired lease；
- worker crash between provider result and DB commit；
- DB busy/locked；
- disk low/full/write failure；
- corrupt WAV/segment/manifest/summary；
- queue pressure；
- microphone unavailable/device loss simulation；
- actual sleep/device/permission 状态若无人工配合则 NOT TESTED；
- End Class during backlog；
- Summary invalid；
- Cleanup partial failure。

要求：
- 无 data-loss shortcut；
- recovery choice 可见；
- resource ownership 清晰；
- task/thread/file/DB handles 最终释放；
- ordinary logs content-free；
- 每个确认修复有 regression test。

==================================================
12. Phase 9 — Soak
==================================================

实现并运行可无人值守的 accelerated full-pipeline equivalent：
- 2–3 小时等效课堂；
- 1000–3000 segments；
- deterministic mock ASR/translation/summary；
- latency、429、500、timeout、network/restart injection；
- ordering、duplicate、backlog、memory、queue、FD、DB/WAL、summary、synthetic purge。

若有硬件录音授权，可同时完成 capture-only real 3h；否则 NOT TESTED。
真实 MiMo short run 只在安全 key + 用户授权存在时运行。

不能用 accelerated full pipeline 代替：
- real target-macOS microphone wall-clock soak；
- real MiMo short test；
- user-coordinated sleep/device/permission matrix。

==================================================
13. Phase 10 — Final Audit Candidate
==================================================

生成 Definition-of-Done evidence map：
- 每一项对应文件、测试、命令、artifact 和 evidence class。
- 没证据就写 NOT TESTED / BLOCKED。
- 不自签 Reviewer/Validator/Oracle PASS。
- 扫描 secrets、课堂内容、runtime artifacts、录音、DB、summary fixtures。
- 检查 Git status 和所有 untracked/staged 文件。
- 检查 docs 与实际代码/状态一致。
- 检查所有 Phase reports 和 known failures 完整保留。
- 输出 `reports/FINAL_BUILDER_HANDOFF.md`。

最终措辞只能是：
“Full implementation candidate completed; independent review required.”

不得说：
“Production ready” / “All phases accepted” / “完全通过”，
除非 Codex Gatekeeper、独立 Validator、Oracle 和用户之后真的批准。

==================================================
14. 麦克风、权限、睡眠与外部调用
==================================================

默认：
HARDWARE_CAPTURE_ALLOWED=false
REAL_MIMO_ALLOWED=false
MANUAL_TCC_CHANGE_ALLOWED=false
MANUAL_SLEEP_DEVICE_ACTION_ALLOWED=false
REAL_DATA_PURGE_ALLOWED=false

缺少授权时，不要停掉整晚任务；实现完整功能，用 deterministic mocks、synthetic
fixtures 和 contract tests 继续。只把外部证据标 NOT TESTED。

只有用户明确写出以下句子，才可跑真实三小时麦克风：
“我授权今晚在这台 Mac 上使用内置麦克风录制环境声进行测试；电脑保持接电、开盖。不得上传、播放或删除录音。HARDWARE_CAPTURE_ALLOWED=true。”

真实 MiMo 需要单独授权和已安全配置的 key。绝不向用户索要在聊天中粘贴 key。

==================================================
15. 夜间工作和资源安全
==================================================

- 长任务使用新的、精确的 `/private/tmp/classroom-interpreter-*` root。
- 不覆盖历史 evidence。
- 定期记录 content-free metrics。
- 监控 disk free，避免填满磁盘。
- 不无限积累 stdout/stderr 到 RAM。
- 不并发争用同一麦克风。
- 不误杀其他 PID。
- 卡死时先记录 stack/state，再只终止本次测试进程。
- 不删除已有 `/private/tmp/classroom-phase2-*` 目录。
- 不自动 commit；仓库没有 baseline，先把完整状态交给用户决定首次 commit。

==================================================
16. 第二天交付物
==================================================

必须交付：
- `reports/PHASE_2_BUILDER_REPORT.md`
- `reports/PHASE_3_BUILDER_REPORT.md`
- `reports/PHASE_4_BUILDER_REPORT.md`
- `reports/PHASE_5_BUILDER_REPORT.md`
- `reports/PHASE_6_BUILDER_REPORT.md`
- `reports/PHASE_7_BUILDER_REPORT.md`
- `reports/PHASE_8_BUILDER_REPORT.md`
- `reports/PHASE_9_BUILDER_REPORT.md`
- `reports/PHASE_10_BUILDER_REPORT.md`
- `reports/FINAL_BUILDER_HANDOFF.md`

每份报告包含：
- scope；
- files changed；
- decisions；
- exact commands/results/counts/durations；
- evidence classification；
- current source hashes；
- artifact paths；
- failures/unknowns；
- privacy/purge impact；
- open gates。

最终还要提供：
1. 完整 `git status --short`；
2. 所有新增/修改文件清单；
3. 完整 test matrix；
4. frontend typecheck/build 结果；
5. backend full test 结果；
6. accelerated soak 指标；
7. real hardware/MiMo 是否运行；
8. 所有 BLOCKED/NOT TESTED；
9. 给 Codex Gatekeeper 的优先审查顺序；
10. 明确声明没有自签验收。

完成后停止，让 Codex 开始独立找问题。不要继续隐藏式重构。
```

## 建议同时发送的补充句

```text
不要为了“今晚做完”写一个巨大单体。继续沿用现有 Runtime Agent、SQLite durable queue、provider port、state machine 和 exact-manifest cleanup 边界。每个 Phase 独立 checkpoint；明天即使有问题，也要能快速定位到具体 Phase、模块、状态转换和证据。
```

