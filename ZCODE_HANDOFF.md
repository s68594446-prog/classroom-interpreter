# Classroom Interpreter — Zcode 完整接管说明与主提示词

> 交接状态：`Phase 2 — Audio Capture Spike` 尚未验收，当前结论为 **FAIL / BLOCKED BY EVIDENCE**。  
> Zcode 角色：**Builder（实现与自测）**。  
> Codex 角色：**独立 Reviewer / Validator Gatekeeper（只检查把关，不继续实现）**。  
> 未经用户和独立 Gatekeeper 明确批准，不得进入 Phase 3。

## 1. 使用方法

1. 把整个仓库 `/Users/shh/Documents/ChatGPT/STUDY` 交给 Zcode。
2. 把下面“第 2 节完整主提示词”原样发送给 Zcode。
3. 要求 Zcode 先做只读接管审计，返回它理解的范围、状态和执行计划；不要让它直接重写。
4. Zcode 每完成一个可审查批次，提交：文件差异、精确命令、完整测试计数、实机证据路径、失败项和未知项。
5. 把 Zcode 的结果交给 Codex 独立检查。Codex 不负责替它修复；若失败，退回 Zcode 修复。

用户已在 2026-09-08 明确授权 Zcode 连续完成整个项目。使用
[`ZCODE_FULL_PROJECT_OVERNIGHT_PROMPT.md`](ZCODE_FULL_PROJECT_OVERNIGHT_PROMPT.md)。
它允许 Zcode 按顺序连续实现 Phase 2–10，不再等待每个 Phase 的中间批准；每个 Phase
仍必须独立留 checkpoint 和证据，且实现不等于验收。真实录音、真实 MiMo、系统权限
变化和真实数据删除仍需各自明确授权。

## 2. 给 Zcode 的完整主提示词（可直接复制）

```text
你现在接管一个已经进行到 Phase 2、但尚未通过 Phase 2 验收的项目：

项目名：Classroom Interpreter
仓库：/Users/shh/Documents/ChatGPT/STUDY
目标平台：Apple Silicon MacBook Air / macOS

你的角色是 Builder：负责实现、修复、自测、整理证据。
你不能同时充当最终 Reviewer、Validator 或 Oracle，也不能自行宣布 Phase 通过。
Codex 从现在起只负责独立检查把关。用户是最终批准人。

==================================================
A. 接管后的第一条铁律
==================================================

不要立刻改代码。

先完整阅读：
1. AGENTS.md（最高优先级仓库规则）
2. ZCODE_HANDOFF.md（本交接文件）
3. ARCHITECTURE.md
4. PRODUCT.md
5. DATA_LIFECYCLE.md
6. FAILURE_MODES.md
7. TEST_PLAN.md
8. DECISIONS.md
9. PHASE2_REVIEW.md
10. tests/hardware/README.md

然后执行只读接管检查：
- git status --short
- git status --branch --short
- 列出相关源文件和测试文件
- 检查当前源码 hash
- 检查现有临时证据目录，但不要播放、上传、打印或删除任何录音
- 运行不打开麦克风的确定性测试

先返回：
- 你理解的当前 Phase
- 允许做什么
- 禁止做什么
- 当前已完成什么
- 哪些证据仍缺失
- 你准备按什么顺序完成

确认范围后才能开始修改。

==================================================
B. 项目总目标
==================================================

学生带 MacBook Air 上 2–3 小时英文课程：

Mac 麦克风持续采集
→ 英文 ASR
→ 简体中文翻译
→ 实时显示英文与中文
→ End Class 后等待全部工作完成
→ 根据英文事实源生成结构化中文总结
→ 原子保存并读回验证总结
→ 只有验证成功后才清除原始和中间课堂数据

最终长期保留的课堂内容只有 Final Summary。
允许另存严格白名单、完全不含课堂文字的 purge metadata。

V1 是 local-first with cloud AI processing，不得宣称 100% local。

==================================================
C. 冻结原则
==================================================

必须持续满足：

PERSIST FIRST, PROCESS SECOND
NO VALID SUMMARY = NO PURGE

其他冻结规则：
- 不扩大 V1 范围。
- Material architecture change 必须先写 DECISIONS.md，再实现。
- Audio capture 不等待网络。
- Provider call 永远不在 SQLite 写事务中。
- SQLite durable queue / rows 是 source of truth；内存队列只作唤醒提示。
- Audio callback 必须极小、非阻塞，不写 SQLite、不做网络、不做 VAD、不做重采样、不重日志。
- 队列满、设备丢失、callback stall、磁盘失败不得静默丢音。
- Runtime Agent 只能依赖 shared contract/public port，不能依赖其他 Agent 私有模块。
- 日志不得包含 API key、录音、transcript、translation、summary input 或课堂内容。
- 不得静默切换麦克风、模型或 provider。
- 清理只能接收精确且验证过的 session manifest，不能递归删除应用根目录或 summary store。
- Mock 必须可注入、确定性、固定 seed。
- 所有测试证据必须标注：local deterministic、synthetic、simulated provider、real MiMo、target macOS 或 NOT TESTED。
- 单元测试绿灯不等于产品通过。

==================================================
D. Phase 顺序（不可跳级）
==================================================

Phase 0 — Architecture：已批准
Phase 1 — Skeleton：已批准
Phase 2 — Audio Pipeline / Audio Capture Spike：当前进行中，尚未批准
Phase 3 — ASR
Phase 4 — Translation
Phase 5 — Live UI
Phase 6 — Final Summary
Phase 7 — Data Purge
Phase 8 — Reliability
Phase 9 — 3 Hour Soak Test
Phase 10 — Final Audit

虽然 Phase 9 还有全产品三小时 soak，当前用户给 Phase 2 的明确验收也要求一次
capture-only 的真实三小时 target-macOS 麦克风测试。两者不是同一个门槛。

你现在只能完成 Phase 2。

禁止在 Phase 2 做：
- real MiMo ASR
- 语义/VAD segmentation 的最终实现
- translation
- summary
- cleanup/purge
- complex UI
- cloud deployment
- 新 provider
- diarization
- 与音频验证无关的重构、装饰或新功能

==================================================
E. 当前 Phase 2 目标
==================================================

唯一核心问题：

MacBook 能否通过候选 Python/CoreAudio 路线，稳定、可恢复、无静默丢失地连续采集三小时课堂音频？

候选路线：
- sounddevice 0.5.6 / PortAudio / CoreAudio
- Python 3.13
- built-in MacBook Air microphone
- native 44.1 kHz mono signed PCM16 WAV
- SoXR HQ streaming normalize 到 16 kHz mono signed PCM16
- 20 秒 rolling recovery chunks

只有完整证据通过，才能把 Python backend 的 ADR 从 candidate 攓为 Accepted。
如果 Python 路线在权限、重连或长时稳定性上确实失败，才允许先写 ADR，再考虑
Swift / AVFoundation capture helper + narrow IPC。不得因为偏好或安装方便直接上 Swift。

==================================================
F. 当前已经实现的内容
==================================================

主要 Phase 2 文件：
- backend/classroom_interpreter/audio/__init__.py
- backend/classroom_interpreter/audio/__main__.py
- backend/classroom_interpreter/audio/durable.py
- backend/classroom_interpreter/audio/listener.py
- backend/classroom_interpreter/audio/normalize.py
- backend/classroom_interpreter/storage/database.py
- backend/classroom_interpreter/api/health.py
- backend/migrations/002_capture.sql
- backend/migrations/003_capture_clock.sql
- tests/audio/test_capture.py
- tests/failure/test_audio_failures.py
- tests/failure/test_audio_process.py
- tests/hardware/README.md
- tests/integration/test_schema.py
- PHASE2_REVIEW.md

当前实现具备：
- `devices | record | synthetic | recover` 音频 CLI。
- 默认/指定设备枚举和选择；不静默 fallback。
- native-rate PCM16 mono capture。
- callback → bounded queue → single consumer/writer。
- queue pressure / backend / writer failure fail closed。
- 20 秒 recovery WAV；pause/stop 封存有效尾块。
- WAV 临时写入、flush、fsync、sidecar、atomic rename、directory fsync、checksum、
  full WAV read-back、SQLite metadata commit。
- 只有 final WAV + verified metadata commit 才算 AUDIO_CAPTURED。
- rename-before-DB crash window 的 sidecar recovery。
- 无法验证的 `.tmp` 保留并阻塞自动恢复；不得盲删。
- capture epoch、intentional pause interval、explicit gap/unknown gap。
- monotonic + wall clock；跨重启 monotonic clock identity 不得伪造连续性。
- SoXR streaming normalization，只消费 committed raw chunks。
- pause/resume、Ctrl+C、SIGTERM、SIGKILL 后恢复相关路径。
- `caffeinate -i -w PID` 防 idle system sleep；实际睡眠仍必须记 gap。
- metrics/report JSON 只含计数与时间，不含课堂内容。
- `/health/capture` 只报告显式注入实例；普通 web shell 不拥有麦克风。
- schema-aware migration 003，兼容两种实验 v2 数据库，并保留旧 manifest 的未知 clock ID。

当前 durability 顺序不要随意改变：

memory frame
→ temp WAV
→ valid WAV header
→ flush/fsync
→ durable sidecar
→ atomic WAV rename
→ directory fsync
→ checksum/read-back
→ SQLite WAL metadata commit with synchronous FULL
→ AUDIO_CAPTURED

Phase 2 的 normalized output 是给后续 Segment Agent 的边界，不代表 Phase 3 已实现。
当前 downstream availability 最多有一个 20 秒 chunk 的粒度延迟；Phase 3 开始前必须
单独审查实时延迟设计，不能偷偷在 Phase 2 引入未批准的新架构。

==================================================
G. 已知历史故障和已经做过的修复
==================================================

必须保留这些失败证据，不能只报告最终绿灯：

1. 最早一次 30 秒 pause 测试 FAIL：expected duration 错把启动时间算作应录音时间。
   已把 active interval、startup、drain 分开；后续短测通过。

2. `stream.stop()` 在真实 CoreAudio SIGINT 测试中超过 90 秒死锁。
   栈显示 PortAudio/CoreAudio `AudioOutputUnitStop` / FinishStoppingStream 相关等待。
   当时只能对该测试进程 SIGKILL；其已封存的前块可恢复，尾部 `.tmp` 被保留。
   修复策略是停止 ingress 时用 `stream.abort()`，再 drain 已排队帧并 seal。
   后续真实 SIGINT/SIGTERM 短测可清洁封存尾块。

3. 独立审查曾发现并修复：
   - normalizer constructor 在 fault boundary 外；
   - async lifecycle race；
   - stop exception 可跳过 close；
   - queue fault 的 gap 可见性不足；
   - 被拒绝的第二 capture instance 可错误修改 active session；
   - stream/state 检查并发不安全；
   - 跨系统启动直接相减 monotonic timestamp；
   - lock reject 后 CLI 仍可能执行不恰当 recovery/report；
   - 实验 v2 schema 有/无 `monotonic_clock_id` 的迁移兼容；
   - 旧 sidecar 缺 clock ID 时不能借用本次启动 ID 制造连续性。

4. 当前开发 `.venv` 曾出现 macOS Documents/隐藏 flag/文件读取卡顿。
   `PYTHONPATH=backend` 是当前可复现的导入方式。临时隔离 venv 测试正常。
   这是 packaging/dev-environment 问题，不能伪装成已解决。

5. 90 个 chunk 的 recovery 验证曾可完成，但一次进程退出在 Python/CFFI/dyld
   finalization 附近异常缓慢。若可复现，必须定位；不得用强杀掩盖正常退出问题。

==================================================
H. 当前可确认的证据（接管时点）
==================================================

1. 最新确定性测试（当前源码）：

命令：
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest

结果：
63 passed, 1 warning in 4.41s

warning：Starlette TestClient 使用 AnyIO 已弃用 BlockingPortal alias；属于依赖告警，
不是本项目测试失败，但必须记录。

2. 真实设备枚举/短 callback：
- host 可见 built-in `MacBook Air的麥克風`，default input index 2。
- native 44,100 Hz，mono。
- 三秒 probe 收到 131,584 frames，0 status callbacks。
- sandbox 内枚举曾为空；只能证明 host 获得当前麦克风权限，不能证明首次请求、拒绝、
  运行中撤销或最终 packaged app 权限流程。

3. 旧源码 hash 对应的真实 30 分钟测试：
- evidence root: /private/tmp/classroom-phase2-30min-final
- session: 968618b3-f7a3-4b94-847e-9dc10ee0374f
- wall clock: 1800.591938583 s
- expected active audio: 1800.03969575 s
- captured audio: 1799.88 s
- duration deficit: 0.15969575 s
- frames: 79,374,708
- verified readable chunks: 90，sequence continuous
- callback overruns: 0
- gaps: 0；unknown gaps: 0
- queue HWM: 4 / 128
- initial/final/peak RSS: 52.78125 / 25.046875 / 52.78125 MiB
- metric samples: 360
- state: STOPPED
- capture_test_pass: true

重要：这次 30 分钟 run 的 `durable.py` hash 是
`ceb9643bcec2811258765d7503d348fc3f031ccfc78865ea975cb932a487a29b`。
当前 `durable.py` 已变为
`8a333fe6f48bba111a5a23642200822d616136409c853300dd76d894165668a5`。
因此该结果是重要历史证据，但不是“当前完整源码”的最终验收证据。当前源码必须重跑。

4. 同一旧 audio source 的 accelerated 3-hour-equivalent：
- root: /private/tmp/classroom-phase2-final-postreview-synthetic
- session: ba676ebc-9080-4864-8319-5bc287c16418
- 10,800 s input，518,400,000 native frames，172,800,000 normalized frames
- 540 chunks，0 overrun，0 gap，HWM 64/128
- elapsed 18.988922792 s
- initial/final/peak RSS: 49.84375 / 30.65625 / 50.765625 MiB

它也需要在当前最终源码上重跑。

5. 同一旧 audio source 的 pause/resume：
- root: /private/tmp/classroom-phase2-pause-final
- session: eb8a7dd8-c95e-4904-a756-f2f78eee5545
- wall 30.5280 s；expected active 26.5018 s；captured 26.44 s
- 3 chunks；0 overrun；0 gap；HWM 2/128；STOPPED

6. 同一旧 audio source 的 signal 短测：
- SIGINT session: 01ce98dc-5440-4321-b5c9-228893661ea8
- SIGTERM session: f7b427fa-e012-4e84-99b7-bdfb5ba5d472
- 分别清洁封存约 7.60 s / 7.36 s 音频为一个 chunk，并进入 STOPPED。
- report 的 `capture_test_pass=false` 是因为请求时长为 120 s、测试被信号主动中断；
  不能把该字段误解为 signal cleanup 失败。

7. 当前源码关键 hash：
- audio/__init__.py: 3095b72f3ae75a256c48011c4935db03182a4d094a4cbd92b7d6a1afd1c7ce2a
- audio/__main__.py: 00e5d8dc81a97934e06eb31d8e33816e08be8bd2cc87315ef3545cfdde7aa96b
- audio/durable.py: 8a333fe6f48bba111a5a23642200822d616136409c853300dd76d894165668a5
- audio/listener.py: 03142dbbcefa7aa32a2156c3276cb23388dc1b5c7eee27fcd4e98371edcbdf18
- audio/normalize.py: bc1af74b8cd70a55b9adc1c01b72d7e6e3066ab4e66394776cf34b7b2071ce60
- storage/database.py: 1ad63b3126b1361fef96382f38811747dde6b56b267ca020c66f2e46ce098a4f
- migrations/001_initial.sql: fabf7fc4eccb8dd9f8c809217fff809d7b35297d0d658bf30a89f2583197e6c4
- migrations/002_capture.sql: 5644413ec9886c317d228a0c8f685c7cb48d8acf63cdf4cdf5ceb1bb4dfb9a97
- migrations/003_capture_clock.sql: 643f0c0687ddbb722c4f78b01f09206c69aac71f34038343cae818e0d0639886

只要任何关键文件变化，旧硬件 evidence 就必须标为 prior-source evidence，不能冒充当前证据。

==================================================
I. 当前仍然阻塞 Phase 2 的事项
==================================================

以下全部完成前，Phase 2 必须保持 FAIL：

1. 在当前最终源码上重跑 accelerated 3-hour-equivalent。
2. 在当前最终源码上重跑真实 pause/resume。
3. 在当前最终源码上重跑真实 Ctrl+C 与 SIGTERM cleanup/recovery。
4. 在当前最终源码上重跑 5 分钟 target-Mac capture。
5. 在当前最终源码上重跑 30 分钟 target-Mac capture。
6. 用户此前明确说“三小时测试稍后再跑”；没有用户新的明确时间安排前不要擅自开始。
7. 得到用户允许和配合后，完成真实三小时 target-Mac 麦克风 capture-only 测试。
8. 手动验证 macOS 权限矩阵：首次请求、拒绝、重试、运行中撤销、恢复。
9. 手动验证 device loss/reconnect；不得静默换设备。
10. 手动验证 display sleep、idle-sleep prevention、lid close/actual sleep、wake 和 gap truthfulness。
11. 复核长 recovery scan / Python finalization 慢退出问题是否可复现。
12. 当前代码完成独立 Reviewer 检查。
13. 独立 Validator 按环境、命令、时长、结果和 artifact 路径复验。
14. Oracle Reviewer 从学生真实上课可用性角度检查权限、暂停、恢复、可见告警和数据真实性。
15. 最后才能决定 D-024：Python ACCEPTED 或 REJECTED → AVFoundation helper。
16. 更新 PHASE2_REVIEW.md；旧文中的 `55 passed`、`5 min IN PROGRESS`、`30 min NOT RUN`
    已过时，不能照抄为当前结论。

==================================================
J. Zcode 的执行顺序
==================================================

Step 1 — Read-only takeover audit
- 阅读全部治理文件。
- 检查 git status、文件树、测试树、hash、忽略规则。
- 不改变架构，不删除临时数据。

Step 2 — Establish a reproducible clean test environment
- 优先用现有 lockfile 和隔离 venv。
- 不要依赖卡顿的现有 `.venv` 得出代码失败结论。
- 不升级依赖，除非有确定原因、最小 diff 和 ADR/说明。
- 所有命令显式使用 `PYTHONPATH=backend`，直至 packaging 问题被单独批准修复。

Step 3 — Verify current deterministic baseline
- 完整 pytest。
- compileall。
- lock check。
- config/schema/idempotency/failure/process tests。
- secret/content scan。
- 检查 untracked 文件；本仓库目前 `No commits yet on main`，所有文件均未跟踪。
  不得假设存在可回退 baseline，不得 reset/checkout 覆盖用户文件。

Step 4 — Code review before hardware work
重点检查：
- callback 是否真的非阻塞、无网络/SQLite/重采样。
- bounded queue overflow 是否 fail closed 且 gap 可见。
- 单一麦克风 ownership 和生命周期锁。
- cancellation/start/stop/pause/resume race。
- `abort → drain → seal → close` 的所有 exception path。
- temp/sidecar/final/DB 的 crash window。
- migration 001/002/003 从空库及两种 v2 旧库升级。
- legacy manifest 缺 clock ID 时是否保持 unknown。
- monotonic clock 跨 reboot 是否会伪造连续性。
- SQLite connection 是否全部关闭。
- recovery 是否只采用 exact validated files。
- report 是否诚实区分 intentional pause、known gap、unknown gap、interrupted run。
- 日志/错误/report 是否完全 content-free。

Step 5 — Small current-source evidence
- 设备枚举。
- 3 秒 callback probe（只有用户允许录音时）。
- 30 秒 pause/resume。
- Ctrl+C。
- SIGTERM。
- synthetic SIGKILL + recover。
- 每项保存独立 private root 和 source hash。

Step 6 — Accelerated current-source test
- 10,800 秒等效 frames。
- 核对 540 个 20 秒 chunk、sequence、WAV readability、metadata、normalize frame count。
- 报告 elapsed、RSS、CPU、FD/open files、queue HWM、overrun、gap、exception。
- synthetic PASS 绝不等于实机三小时 PASS。

Step 7 — Hardware ladder
- 5 分钟。
- 检查 evidence 后再 30 分钟。
- 检查 evidence 后才准备三小时。
- 每次运行前记录 source hash；每次运行后 read-back 所有 chunk、检查连续序列和 recovery。
- 任一步失败，先停，不要直接用更长时间掩盖失败。

Step 8 — Manual macOS matrix
- 权限改变必须由用户手动配合；不得自行 reset TCC 或修改隐私设置。
- 合盖/系统睡眠必须由用户配合；不得自行让机器睡眠。
- 设备断开/重连必须由用户操作真实设备。
- 每个不可录音区间必须记录真实 gap 或 unknown gap；不得合成静音冒充录音。

Step 9 — Real three-hour run（仅在用户明确允许后）
- Mac 接电、开盖并允许录制环境声。
- 使用独立 private root。
- 启动后核对 `pmset -g assertions`。
- 定期记录 content-free RSS、CPU、FD、open files、queue depth/HWM、disk free。
- 不把任何 WAV、transcript 或课堂内容放入 Git/聊天/云端。
- 结束后 graceful shutdown，完整 recovery/readability/sequence 校验。

Step 10 — Builder self-check report
- 只报告证据，不自我批准。
- 保留失败历史。
- 明确当前源码 hash 与 artifact hash 是否一致。

Step 11 — Independent gates
- 把 diff 和报告交给 Codex Reviewer。
- Reviewer 有 finding：Zcode 只修已确认问题并加 regression test。
- 再交给独立 Validator 和 Oracle。
- 三者均通过且用户批准后，才更新 ADR 与 Phase status。

Step 12 — STOP
- 完成 Phase 2 review 后停止。
- 不自动进入 Phase 3。

==================================================
K. 建议命令
==================================================

普通测试不会打开麦克风：

PYTHONPATH=backend <venv>/bin/python -m pytest
PYTHONPATH=backend <venv>/bin/python -m compileall -q backend
UV_CACHE_DIR=/private/tmp/classroom-interpreter-uv-cache uv lock --check --offline

当前已知可用隔离环境（存在时才用）：

PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest

设备与硬件（必须经过用户允许）：

PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio devices
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --root <PRIVATE_ROOT> --seconds 300
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --root <PRIVATE_ROOT> --seconds 1800
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --root <PRIVATE_ROOT> --seconds 10800
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio record --root <PRIVATE_ROOT> --seconds 30 --pause-at 5 --pause-seconds 3
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio synthetic --root <PRIVATE_ROOT> --seconds 10800
PYTHONPATH=backend uv run --extra audio python -m classroom_interpreter.audio recover --root <PRIVATE_ROOT> --session <SESSION_ID>

不要复制旧 root 作为新 run 的目标。每次创建新的私有目录，避免 artifact 混淆。

==================================================
L. Phase 2 验收标准
==================================================

全部满足才可以建议 PASS：
- backend selection 有直接证据。
- built-in Mac microphone capture 成功。
- Audio Listener Agent contract 可消费 capture stream。
- recovery chunks 与 metadata 正确持久化。
- 半写入文件永不被当作 valid。
- pause/resume 正确，pause 不生成假静音。
- normal stop / Ctrl+C / SIGTERM 正确。
- hard crash 后 finalized chunks 可恢复，未知 temp 不被盲用或盲删。
- continuity/gap 可测且不把 unknown 说成 zero。
- capture health metrics 完整且 content-free。
- current-source 5 分钟实机 PASS。
- current-source 30 分钟实机 PASS。
- current-source 3 小时实机 PASS。
- current-source 3 小时等效 accelerated PASS。
- 没有无界 memory/file/FD/queue 增长。
- 权限行为完成真实验证并记录。
- device loss/reconnect 完成真实验证并记录。
- sleep/wake 行为完成真实验证并诚实记录 gap。
- ADR 完整并由独立审查接受。
- unit/integration/failure/recovery tests PASS。
- Reviewer、Validator、Oracle 均给出 PASS。

不得预先拍脑袋设内存阈值。必须报告 initial/final/peak RSS、warmup 后走势和 slope，
由证据判断是否 bounded/stable。

==================================================
M. 三小时报告必须包含
==================================================

- exact start/end time 与 timezone
- requested duration
- wall-clock duration
- expected active audio duration
- captured audio duration
- duration deficit
- source hashes
- device name / stable identity
- native sample rate / channels / format
- internal sample rate / format / resampler
- initial/final/peak RSS
- warmup 后 RSS range 与 slope
- periodic metric sample count
- CPU median/max（说明采样范围）
- active/end FD and open-file count
- queue capacity / high watermark / final depth
- callback overruns
- gap count / total / largest
- unknown gap count 与 lower-bound 标记
- chunk count / sequence range / continuity
- readable/verified/invalid/temp/unmanifested chunk count
- startup / shutdown drain time
- exceptions and reason codes
- caffeinate assertion evidence
- final session state
- exact commands
- artifact paths
- privacy statement

==================================================
N. Phase 2 最终输出格式
==================================================

CLASSROOM INTERPRETER — PHASE 2 REVIEW

Status:
PASS / FAIL

Source Revision / Hashes:
...

Phase and Scope:
...

Files Changed:
...

Decisions Added or Changed:
...

Selected Audio Backend:
...

Why:
...

Microphone Permission:
...

Capture Format:
...

Device Sample Rate:
...

Internal Sample Rate:
...

Recovery Chunk Duration:
...

Durability Boundary:
...

Hardware Tests:
5 min:
30 min:
3 hour:

3-Hour Metrics:
wall clock duration:
captured audio duration:
initial RSS:
final RSS:
peak RSS:
RSS slope:
queue high watermark:
callback overruns:
gap count:
unknown gap count:
total gap:
largest gap:
chunks:
errors:

Accelerated Test:
...

Pause / Resume:
...

Device Loss:
...

Sleep / Wake:
...

Crash Recovery:
...

Tests Run:
exact commands + exact counts

Evidence Classification:
...

Independent Reviewer:
...

Independent Validator:
...

Oracle Reviewer:
...

ADR:
...

Architecture Deviations:
...

Known Failures and Unknowns:
...

Privacy and Purge Invariants:
...

Blocking Issues:
...

Recommended Next Phase:
Phase 3 — Segmentation + Real MiMo ASR Integration

然后停止，等待用户批准。

==================================================
O. 禁止的捷径
==================================================

不得：
- 因为 63 tests 通过就宣布 Phase 2 PASS。
- 用 accelerated test 代替真实三小时。
- 用旧 source hash 的 30 分钟 run 代替当前 source run。
- 把 device listing 当作 callback capture。
- 把开发宿主权限成功当作 packaged app 权限成功。
- 把 intentional pause、actual sleep 或 unknown discontinuity 隐藏为连续录音。
- 在回调中加入 DB/network/resample/VAD/logging。
- 使用 browser MediaRecorder 作为 production capture，除非先获架构批准。
- 把整堂课存在 RAM 或单一三小时大对象中。
- 静默丢弃 queue overflow 或 device disconnect。
- 自动删 `.tmp`、旧录音或任何用户数据。
- 输出、播放、上传或提交真实录音。
- 为了 demo 好看接 MiMo、翻译、总结或 UI。
- 修改 migration 001 来掩盖 migration 问题。
- 修改验收标准让现有实现通过。
- 自己既当 Builder 又签 Reviewer/Validator/Oracle PASS。
- 在没有 accepted Phase 2 commit report 前进入 Phase 3。
```

## 3. 当前仓库与版本控制状态

接管时实测：

```text
## No commits yet on main
所有项目文件均为 untracked (`??`)
```

这意味着：

- 仓库没有可依赖的 Git baseline，也没有任何 accepted commit。
- `git diff` 不会完整展示 untracked 文件；审查必须直接检查文件内容。
- Zcode 不得使用 destructive reset/checkout/clean。
- 在首次 commit 前必须先完成完整 status、secret/content scan、ignore 验证和 Phase report。
- 是否创建首次 commit 应由用户决定；Builder 不应把“代码存在”等同于“已验收”。

当前 `.gitignore` 已确认至少忽略 `.env`、`*.sqlite3`、`*.wav`。仍需在首次提交前对所有 staged 路径再次检查。

## 4. 当前架构总览

```text
Mac microphone
  ↓ native PCM16 mono at stable device rate
Audio Listener Agent / PortAudio callback
  ↓ non-blocking bounded handoff
Single capture consumer
  ├─ Recovery Chunk Writer → durable raw WAV + sidecar + SQLite metadata
  ├─ continuity/gap accounting
  └─ SoXR streaming normalizer → committed 16 kHz mono PCM16 boundary

Phase 3 later:
  Segment Agent → persistent queue → MiMo ASR

Phase 4 later:
  bounded context → ordered per-session translation

End Class much later:
  drain → English-source-of-truth summary → atomic verify → exact-manifest purge
```

Runtime 是 modular monolith 内的独立职责单元，不是十个网络微服务。浏览器只控制和显示；麦克风必须由 backend 拥有，浏览器刷新不能停止录音。

## 5. V1 明确范围

仅包括：macOS、单一 active session、Mac microphone、English ASR、English → Simplified Chinese、实时英文/中文、MiMo、local SQLite queue、失败恢复、课后结构化总结、总结验证后清理。

禁止扩展：粤语/普通话翻译/其他语言、iOS/Android/Windows、账号/支付/会员、云同步/协作、其他 provider、声纹/diarization、SaaS/microservices/Cloudflare、浏览器扩展、无关动画、法证级安全擦除承诺。

## 6. Gatekeeper 的审查规则

从现在起 Codex 只按以下方式把关：

1. **Scope Gate**：是否只做当前 Phase；越界即 FAIL。
2. **Architecture Gate**：是否改变 frozen invariant；未先写 ADR 即 FAIL。
3. **Code Gate**：并发、ownership、error path、restart、partial write、duplicate、ordering、cleanup containment。
4. **Evidence Gate**：命令、环境、时长、计数、artifact、source hash 是否齐全。
5. **Hardware Gate**：实机证据与 synthetic evidence 分开；旧源码证据不冒充当前证据。
6. **Privacy Gate**：无 key、音频、课堂文字泄漏；不擅自删除真实录音。
7. **Oracle Gate**：学生真实上课时是否知道权限、延迟、暂停、故障、恢复和数据状态。
8. **Phase Gate**：只有所有 blocking gate 关闭才给 PASS。

Gatekeeper 返回 finding 时使用：

```text
[P0/P1/P2/P3] 标题
Evidence: 文件:行号 / 命令 / artifact
Impact: 对数据、隐私、顺序、恢复或用户的影响
Required fix: 最小修复要求
Required regression evidence: 必须新增或重跑什么
```

Builder 修复后必须重新提交 Reviewer → Validator → Oracle，不得只回复“已修”。

## 7. 当前真实结论

截至本交接文件生成时：

- Phase 0：通过。
- Phase 1：通过。
- Phase 2 实现：已有较完整候选代码与历史短/中时实机证据。
- 当前确定性回归：`63 passed, 1 warning in 4.41s`。
- 当前 Phase 2 状态：**FAIL**，原因是当前 source 的硬件证据未全部重跑、真实三小时被用户延期、权限/设备丢失/睡眠矩阵未完成、独立最终 gates 未完成、D-024 尚未接受。
- Phase 3：**禁止开始**。
- MiMo、translation、summary、purge：Phase 2 中均禁止实现或测试。
- 未创建 Git commit。

## 8. 临时证据与隐私

接管时发现的主要私有临时根目录包括：

```text
/private/tmp/classroom-phase2-30min-final                     153M
/private/tmp/classroom-phase2-final-postreview-synthetic      992M
/private/tmp/classroom-phase2-pause-final                     2.4M
/private/tmp/classroom-phase2-signal-postreview               1.4M
/private/tmp/classroom-phase2-hardware                        30M
```

另有其他早期 synthetic/hardware roots。它们可能含真实环境音 WAV，属于敏感用户数据：

- 不上传；
- 不提交 Git；
- 不在聊天中打印内容；
- 不播放；
- 不自动删除；
- 只读取 content-free report/metrics 和文件元数据；
- 需要清理时必须另行取得用户明确授权，并列出精确路径。

## 9. Zcode 每次交付给 Gatekeeper 的最小材料

- 当前 Phase 和本批次 scope。
- `git status --short`。
- 相关完整 diff；untracked 文件要用可审查方式呈现。
- 修改理由和 ADR 影响。
- 精确测试命令与原始计数。
- 失败测试，不得省略。
- current source hashes。
- 每个硬件 run 的 root、session ID、report path、时长、结果。
- evidence classification。
- 隐私说明。
- blocking unknowns。
- 明确声明未进入下一 Phase。

## 10. 交接后的职责边界

```text
用户：决定产品范围、允许实机敏感操作、批准 Phase
Zcode：Builder / Repair Agent
Codex：Reviewer / Validator Gatekeeper，只检查，不代写功能
Oracle：独立检查学生是否真的能完成 2–3 小时课堂
```

若 Zcode 遇到架构冲突，它必须停下并提出 Decision Request，而不是自行扩大范围。若实机操作涉及麦克风权限、录制环境声、合盖/睡眠或真实设备断开，必须先让用户明确配合。
