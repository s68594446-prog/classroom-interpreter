# Claude Code 接管主提示词 — Classroom Interpreter

> 使用方式：把本文件全文粘贴给 Claude Code，并让它在
> `/Users/shh/Documents/ChatGPT/STUDY` 中工作。
>
> 本文件是项目交接上下文，不是验收结论。任何“已实现”都必须由 Claude
> 重新读取实际代码和运行测试确认；任何 Builder/Repair Agent 都无权自行宣布
> Reviewer、Validator 或 Oracle 通过。

---

## 0. 你的身份与本轮目标

你现在接管一个已有实现和已有审查历史的项目：**Classroom Interpreter**。

你的角色是：

1. 首先作为 **接管工程师 / Repair Agent**，保护当前工作区并准确恢复上下文；
2. 完成仍未完成的两个主要阻塞项；
3. 检查此前 1–9 项修复候选没有被后续改动破坏；
4. 运行确定性回归、前端构建和新的有效加速 soak；
5. 输出可供 Codex 独立复审的代码、测试与证据。

你不是从头重新设计，也不能自行宣布项目 PASS。完成后必须交回 Codex
独立复审。

只操作这个仓库：

```text
/Users/shh/Documents/ChatGPT/STUDY
```

不要访问、修改或删除项目外文件。运行时测试数据只能放在 pytest 临时目录、
`/private/tmp` 中新建的任务专用目录，或仓库明确的测试 artifact 目录。不要碰用户
真实课堂数据。

---

## 1. 接管前必须先做的事

不要立刻改代码。先在仓库根目录执行并保存输出：

```bash
pwd
git status --short
git log --oneline --decorate -10
git diff --check
git diff -- backend/classroom_interpreter/storage/repository.py \
  tests/integration/test_repository_idempotency.py \
  tests/integration/test_translation_pipeline.py
sed -n '1,240p' tests/integration/test_worker_count_wiring.py
```

当前已知 Git 状态（2026-09-10 接管前实测）是：

```text
HEAD 4948567 partial repair handoff snapshot
parent ee72af2 pre-review snapshot

 M backend/classroom_interpreter/storage/repository.py
 M tests/integration/test_repository_idempotency.py
 M tests/integration/test_translation_pipeline.py
?? tests/integration/test_worker_count_wiring.py
```

这些未提交修改很可能是 Zcode 在 token 用尽前留下的第 3 项修复，不得丢弃、
覆盖或 reset。先检查它们是否含秘密、真实音频、运行时数据库或大型产物；如果
没有，就先创建一个仅用于回退的快照提交，例如：

```bash
git add backend/classroom_interpreter/storage/repository.py \
  tests/integration/test_repository_idempotency.py \
  tests/integration/test_translation_pipeline.py \
  tests/integration/test_worker_count_wiring.py
git commit -m "pre-claude-takeover snapshot"
```

这个提交不是接受这些改动，只是保护现场。不要用 `git reset --hard`、
`git checkout --` 或删除未跟踪文件。

如果实际状态与上面不同，以你现场读到的状态为准，并在报告中解释差异。

---

## 2. 产品目标与 V1 边界

这是一个 macOS 本地优先的课堂口译应用：

1. 后端采集英文课堂音频；
2. 持久化后再处理；
3. VAD 生成语义 segment；
4. MiMo ASR 生成英文事实文本；
5. MiMo 翻译生成简体中文实时内容；
6. React 页面显示课堂状态、英文和中文内容；
7. 课程结束后，以英文 ASR 为事实来源生成结构化总结；
8. 只有总结被安全写入、验证并登记后，才允许清理音频、转录、翻译等临时内容；
9. 最终长期保留的课堂内容只能是最终 Markdown 总结，另可保留严格白名单的
   无课堂内容 metadata ledger。

V1 明确不扩展：

- 不增加其他 provider、语言或平台；
- 不做云部署、多账号、说话人分离或装饰性功能；
- React 只负责控制和显示，麦克风由后端持有；
- 服务只绑定 `127.0.0.1`；
- 项目是 local-first，但 MiMo 会处理必要音频/文本，不能宣传成 100% 本地；
- 除非用户另行明确授权，不进行真实 MiMo、真实三小时麦克风、隐私权限变更、
  设备拔插、合盖/睡眠或真实数据 purge。

两个最高优先级不变量：

```text
PERSIST FIRST, PROCESS SECOND
NO VALID SUMMARY = NO PURGE
```

进一步要求：

- 英文 ASR 是最终总结的事实来源；中文翻译只用于组织和阅读辅助；
- SQLite durable queue / lease / cursor 是恢复的真实来源；
- provider 网络调用不能放在 SQLite 写事务内；
- ordinary logs 必须 content-free，不记录 API key、音频、转录、翻译、prompt、
  summary body；
- cleanup 只能按精确 manifest 删除 session 范围内文件，必须 canonicalize 路径
  并拒绝 symlink；绝不能广泛递归删除应用根目录或 summary store；
- mock 必须 deterministic 且可注入，不能静默 fallback 成“成功”；
- 任何模拟测试、合成 soak 或绿色单元测试都不能表述成真实硬件/真实 provider 验收。

---

## 3. 技术栈与目录

后端：Python `>=3.12`、FastAPI、Pydantic、SQLite。

前端：React 19、TypeScript、Vite。

关键目录：

```text
backend/classroom_interpreter/
  agents/             # segmentation, transcription, translation, context,
                      # summary, cleanup, health
  api/                # REST, health, WebSocket
  audio/              # listener, durable chunk writer
  domain/             # 状态和模型
  providers/          # MiMo adapters 与 mock
  runtime/pipeline.py # SessionOrchestrator 与 worker loops
  storage/            # Database 与 SessionRepository
backend/migrations/   # 当前至少到 migration 005
frontend/             # React/Vite UI
scripts/soak_accelerated.py
tests/
reports/
```

配置快照：

```text
APP_HOST=127.0.0.1
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_ASR_MODEL=mimo-v2.5-asr
MIMO_TRANSLATION_MODEL=mimo-v2.5
MIMO_SUMMARY_MODEL=mimo-v2.5-pro
ASR language=en
```

API key 不得写进聊天、日志、fixture、截图、报告或 Git。不要向用户索要 key。
如果没有安全注入的 key，就把真实 MiMo 标为 `NOT TESTED`。

正式运行时数据设计在：

```text
~/Library/Application Support/Classroom Interpreter/
```

但是本轮测试不得修改用户真实运行目录，应通过 Settings 指向临时目录。

---

## 4. 必读文档顺序

按以下顺序阅读实际文件，不要只读报告摘要：

1. `CLAUDE_CODE_MASTER_HANDOFF.md`
2. `reports/INDEPENDENT_REVIEW_2026-09-08.md`
3. `REPAIR_REPORT.md`
4. `DECISIONS.md`，尤其 D-032 到 D-038
5. `ARCHITECTURE.md`
6. `DATA_LIFECYCLE.md`
7. `FAILURE_MODES.md`
8. `TEST_PLAN.md`
9. `PRODUCT.md`
10. `README.md`
11. `reports/FINAL_BUILDER_HANDOFF.md` 和 Phase 2–10 Builder reports，仅作为历史

注意：

- `reports/INDEPENDENT_REVIEW_2026-09-08.md` 是独立审查事实源，结论为 FAIL；
- Builder reports 是实现者自己的陈述，不能当成独立证据；
- `REPAIR_REPORT.md` 是部分修复交接，当前有两处明显过时：它写着所有改动未提交，
  但现在已经有 `4948567` 快照；它还没有包含最新未提交的第 3 项修改；
- 仓库的 `AGENTS.md` 保存了项目历史治理和安全约束。用户已经说明此前聊天层面的
  AGENTS 指令不再单独生效；但其中与产品架构、数据安全、证据分级和禁止自验收有关
  的内容仍是本项目既定要求，除非本提示词明确修改。

---

## 5. 完整项目历史与聊天决策

### Phase 0–1

- 先冻结了架构、任务边界、状态机、数据生命周期、失败模式与验收标准；
- 采用本地 modular monolith，并把 runtime agents、数据层和开发 Harness 角色分开；
- Phase 1 建立后端、前端、SQLite schema、端口和 deterministic mocks；
- 独立 Builder / Reviewer / Validator / Oracle / Repair 的职责不能混为一体。

### Phase 2 音频采集

- 选择 Python `sounddevice` / PortAudio / CoreAudio 候选；
- callback 只做非阻塞复制，consumer 负责 durable chunk；
- recovery chunk 为原生 44.1 kHz PCM16，committed chunk 规范化为 16 kHz mono；
- 20 秒 chunk 只有在 flush/fsync、sidecar、atomic rename、checksum/read-back、
  directory sync、SQLite metadata commit 之后才算 `AUDIO_CAPTURED`；
- 发现 `Pa_StopStream` / CoreAudio stop 在真实 SIGINT 场景可能死锁，改用 abort +
  queue drain，约 0.2–0.3 秒退出且保留队列音频；
- 已有 30 分钟真实麦克风证据：约 1799.88 秒、90 chunks、sequence 0–89、
  0 gap、0 overrun、资源有界；
- 已有合成 10800 秒等效音频：540 chunks、0 gap/overrun；
- 这些不等于真实三小时验收。真实三小时麦克风、权限拒绝/撤销、设备断开重连、
  sleep/wake 仍然没有验证。

### Zcode 过夜实现 Phase 3–10

Zcode 曾报告：173 个 deterministic tests 全绿、前端 build 通过，以及一个
9000 秒等效 / 1800 segments / 74 次故障注入的加速 soak。它同时声明没有
自我验收，真实 MiMo 和真实硬件仍未测试。

这些数字后来被 Codex 独立审查发现不能全部作为可靠证据，尤其旧 soak 的
restart/backlog 场景并未让 Segmenter 在生产过程中真正处理积压。

### Codex 独立审查

Codex 在任何审查动作前建立：

```text
ee72af2 pre-review snapshot
```

它独立重跑当时后端 `173 passed` 和前端 typecheck/build，并通过代码阅读、
竞态探针和 mutation testing 得出 FAIL，列出 10 项 BLOCKING ISSUES。

### 部分 Repair 阶段

随后已生成修复候选并建立：

```text
4948567 partial repair handoff snapshot
```

最后一次已记录的完整后端测试是：

```text
184 passed, 1 upstream Starlette/AnyIO warning in 24.19s
```

这只是当时快照的 Repair Agent 结果，不是现在工作树的独立复验。当前工作区增加了
新的未提交改动，必须重新运行。

---

## 6. 独立审查的 10 项阻塞问题及当前候选状态

以下“候选 implemented”不表示已通过，必须检查实际代码和 adversarial test。

### 1. Active DELETE 数据丢失路径 — 候选已实现，待复审

原问题：DELETE recovery API 可以删除仍在录音、FINALIZING 或仍由 runtime
持有的 session。

候选修复：

- 只允许删除 `RECOVERY_REQUIRED` 或从未开始的 `NEW`；
- runtime 正在持有 session 时拒绝；
- RECORDING、PAUSED、FINALIZING、SUMMARIZING、SUMMARY_SAVED、PURGING 均拒绝。

关键测试：

- `test_delete_active_recording_session_is_refused_without_data_loss`
- `test_delete_finalizing_session_is_refused_without_row_loss`

重新引入状态/ownership gate 缺陷时，这些测试必须失败并证明 rows/files 未被删。

### 2. SUMMARY_SAVED 原子性 — 候选已实现，待复审

原问题：状态先提交，summary path/hash 在另一事务写入；中间崩溃会出现假的
`SUMMARY_SAVED`，违反 purge 不变量。

候选修复：文件 fsync、rename、目录 fsync、读回与 hash 验证完成后，summary
path/hash/model/timestamp、session state 和 transition audit 在同一个 SQLite
事务提交。

关键测试：

- `test_summary_metadata_and_saved_state_roll_back_together_on_crash_boundary`

测试需要在 summary session UPDATE 与 audit/commit 边界注入失败，并断言：

- 状态不是 `SUMMARY_SAVED`；
- path/hash 没有半提交；
- purge 被拒绝；
- 重启后进入安全恢复路径。

### 3. 并发翻译 + 独立 Sequencer + worker count — 仍未完成，是当前 P0

原问题：translation 只能 claim `next_translation_sequence`，形成严格队头阻塞；
配置的 worker 数量没有真实创建；没有 durable staging + ordered release。

候选已有：

- migration 005 translation staging/context tables；
- 多个 ASR/translation worker list；
- `TranslationSequencer`；
- slow-head 测试；
- 当前未提交改动把 repository 的直接提交方法重命名为
  `commit_translation_result_direct`，并在测试中标为 legacy direct path；
- 当前未跟踪 `tests/integration/test_worker_count_wiring.py` 尝试验证真实
  `create_app` wiring。

**2026-09-10 现场实测：仍然失败。** 执行：

```bash
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest \
  tests/integration/test_worker_count_wiring.py \
  tests/integration/test_translation_pipeline.py \
  tests/integration/test_repository_idempotency.py -q
```

失败点：

```text
test_production_app_creates_configured_worker_counts
expected runtime.asr_worker_count == 3
actual   runtime.asr_worker_count == 2
```

根因现场可见于 `backend/classroom_interpreter/main.py`：production
`create_app` 构造 `SessionOrchestrator` 时仍未传入：

```text
asr_worker_count=resolved_settings.asr_worker_count
translation_worker_count=resolved_settings.translation_worker_count
```

你必须：

1. 修复真实 `create_app` wiring；
2. 让非默认 3/4 worker 测试通过；
3. 修正 `test_worker_count_wiring.py` 中任何脆弱的 import/path hack，使测试真实、
   可维护且不依赖私有实现超过必要范围；
4. 用 slow N 测试证明 N+1/N+2 的 provider 调用能完成并进入 durable staging，
   同时 public store 仍只按 sequence 发布；
5. 搜索所有 `commit_translation_result_direct` 调用，证明 production worker、API、
   orchestrator 都无法绕过 Sequencer。若该方法只服务 repository contract tests，
   应限制可见性/命名并清楚记录；不要仅靠 docstring 宣称安全；
6. Sequencer commit 后 staging 必须清空或进入明确终态；slow-head 测试要断言无
   staged residue，所有 segment 最终 DONE；
7. 故意恢复 head-only claim、移除 sequencer 或漏传 counts 时，对应测试必须变红。

### 4. 首次翻译上下文快照 — 候选已实现，待复审

原问题：每次 retry 都重新生成最新 context。

候选修复：segment 首次进入 translation 时保存 versioned canonical JSON +
SHA256 context snapshot，所有 retry 使用同一快照。

关键测试：

- `test_translation_retry_reuses_first_attempt_context_snapshot`

测试必须做到：首次 context 为 X，后续 segment 改变最新上下文，再 retry 前者，
断言 provider 第二次仍看到 X，而不是新 context。

### 5. Agent 未预期异常隔离 — 候选已实现，待复审

原问题：Segmenter 非 `CorruptCaptureChunk` 异常会让 task 死亡；其他 loop
可能静默吞异常。

候选修复：每个 runtime worker loop 最外层 catch-all，写 content-free fault
record，bounded wait 后 continue。

关键测试：

- `test_segmenter_unexpected_exception_is_recorded_and_loop_continues`

测试必须注入 `RuntimeError("probe boom")`，确认异常不传播、loop 再次调用、其他
worker 仍活着；日志/数据库不能含课堂内容或 exception payload 中的敏感内容。

### 6. Recovery 重建 segment 边界 — 候选已实现，待复审

原问题：只恢复旧 VAD cursor，没有从最后一个连续 DONE segment 的结束时间点
之后重新跑持久化音频。

候选修复：保留连续 DONE prefix，删除其后的派生 segment rows/files，重置 cursor
到 prefix 边界后的第一个持久化 capture sample，重新 VAD。

关键测试包括：

- `test_restart_rebuilds_open_vad_tail_from_capture_not_saved_cursor`
- 重写后的 crash/restart segmentation test

测试必须模拟崩溃落在未完成 segment 中间，并故意污染保存的 pending PCM/cursor，
确认重启结果与从原始 committed capture 一次性分段一致。

### 7. WebSocket replay→subscribe 丢事件窗口 — 候选已实现，待复审

原问题：先查 DB replay，再 subscribe live；两者间发布的事件既不在查询结果里，
也进不了 live queue。

候选修复：先 subscribe，再查询 persisted replay；replay 期间缓冲 live events，
最后按 sequence 去重回放。

关键测试：

- `test_websocket_buffers_event_published_after_replay_query_before_stream`

必须在 replay query 完成、stream 开始前精确发布事件，并确认重连补齐：无遗漏、
无重复、sequence 连续。只测试静态/无生产者 replay 不足够。

### 8. Summary `.md.tmp` 崩溃残留 — 候选已实现，待复审

原问题：rename 前崩溃留下 `.md.tmp`，重试用 `xb` 永久报
`FileExistsError`。

候选修复：重试前安全清理旧的 regular temp file；temp 若为 symlink 必须 fail
closed；必要时校验/adopt 合法 final file。

关键测试：

- `test_summary_retry_recovers_temp_left_by_crash_before_rename`

### 9. Health/Metrics 写隔离 — 候选已实现，待复审

原问题：没有真正 Health Monitor，也没有独立 DB/内存批量 flush；旧 busy 测试
不是 metrics 隔离测试。

候选修复：`agents/health.py` 使用 bounded memory buffer + dedicated
`health.sqlite3` 批量 flush；健康写不拿 runtime DB write lock；溢出时 drop oldest，
只记录数字 drop count。

必须检查：

- dedicated DB 路径确实不同；
- runtime DB 被 `BEGIN IMMEDIATE` 锁住时，health flush 仍能完成；
- sustained metrics 写入期间 pipeline writes 的 latency 和数据完整性在设定阈值内；
- main runtime DB 的 `metric_samples/agent_heartbeats` 没被 production health writer 写入；
- shutdown 会 flush 或明确报告有界丢弃。

### 10. 加速 soak restart/backlog 证据无效 — 未修，是当前 P0

旧报告的 1800/1800、0 丢失、0 重复、74 次故障恢复不能作为修复后架构证据。

当前 `scripts/soak_accelerated.py` 的实际问题：

- `build_agents()` 创建 `SegmentAgent`、ASR 和 TranslationAgent；
- 但生产阶段 `consumer_cycle()` 只调用 ASR 和 Translation；
- Segmenter 直到所有 chunks 写完后的 drain 才开始处理；
- 没有构建或调用 `TranslationSequencer`；
- restart 只是重新构造 agent objects，不能证明在 Segmenter 正处理 backlog 时被中断；
- `max_backlog` 实际按 consumer steps 计算，不是可靠 durable queue backlog 指标；
- 所以旧脚本无法验证 migration 005 后的真实 pipeline。

必须重写，不能沿用旧漂亮数字。

---

## 7. 新 soak 的强制验收设计

新的加速 soak 必须使用**新的空目录和新数据库**，并把 raw evidence 保存到新的
artifact 目录，不能覆盖或复用旧 Phase 9 报告。

至少满足：

1. chunk producer 与 SegmentAgent 同时运行；
2. SegmentAgent 在生产过程中持续处理 committed capture chunks；
3. ASR workers、Translation workers 和 TranslationSequencer 同时运行；
4. restart 注入前记录并断言：
   - committed chunks > 0；
   - durable segment cursor > 0；
   - 已生成 segments > 0；
   - 至少一个可量化 durable backlog > 0；
   - Segmenter 的确执行过，不是只构造对象；
5. restart 必须发生在 producer 尚未结束且 pipeline 正在处理 backlog 时；
6. 停止/取消旧 worker tasks，重新打开同一数据库并重建 orchestrator/agents，不能
   只在同进程里覆盖三个 Python 变量；
7. recovery 调用 boundary rebuild，并保存：保留 DONE prefix、删除/重建 tail、
   restart 前后 cursor、segments、backlog 的证据；
8. 重启后 producer 继续，所有 stages 继续推进；
9. translation provider 可乱序完成，所有结果先 durable staged，再由 Sequencer
   严格按 sequence 公开提交；
10. 最终 drain 使用生产 runtime orchestration 或等价完整 stage loop，不能漏掉
    Segmenter/Sequencer；
11. 验证 expected segments 与实际 segments 精确匹配、sequence 连续、无 duplicate、
    无 loss、source hash/context hash 合法、staging 无残留、所有 segment DONE；
12. summary 验证完成后，在测试数据上检查 purge 不变量；
13. 采样 RSS、FD、SQLite integrity、queue/backlog high-water marks、fault injection
    counts 和 retry outcome；
14. 明确区分“9000 秒等效合成音频”和“9000 秒挂钟运行”。不要把加速音频时长
    写成真实 2.5 小时运行；
15. 输出机器可读 JSON/JSONL evidence 和人类可读新报告，记录 command、commit、
    Python/macOS 信息、开始/结束时间、wall-clock duration、warning、artifact hash。

先做一个小规模 smoke（例如几十个 cycles）证明 harness 逻辑正确，再跑新的
9000 秒等效音频版本。若脚本失败，保留失败 artifact，不要删除后只展示成功结果。

对于每一个关键 soak assertion，问自己：如果把 Segmenter 从生产 loop 移除、
把 restart 移到生产结束后、漏掉 Sequencer、或只覆盖变量不重启 runtime，这个
assertion 是否一定会失败？如果不会，测试还不可信。

---

## 8. 测试命令与环境事实

这个 checkout 过去在仓库 `.venv` 中出现 Python import stall；独立审查稳定使用：

```text
/private/tmp/classroom-phase2-venv.7Ey9Un/bin/python
```

如果该环境仍存在且依赖完整，可使用：

```bash
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest
```

如果不存在，不要假装测试失败是代码失败；在 `/private/tmp` 新建专用 venv，按
`pyproject.toml`/`uv.lock` 安装依赖，并记录环境准备。不要污染系统 Python。

前端：

```bash
cd frontend
npm run typecheck
npm run build
```

每完成一类修复，至少跑其 focused tests；完成全部后运行：

```bash
PYTHONPATH=backend /private/tmp/classroom-phase2-venv.7Ey9Un/bin/python -m pytest
cd frontend && npm run typecheck && npm run build
git diff --check
```

还要运行：

- migration 从空 DB 到最新版本；
- migration 重复执行/idempotency；
- 旧版本 DB upgrade fixture；
- secret scan（排除文档中纯文字示例，但逐个人工确认 match）；
- 新 soak smoke；
- 新 9000 秒等效 soak。

请报告精确结果，例如：

```text
command
exit code
passed / failed / skipped 数量
warning 数量和内容类别
wall-clock duration
artifact path + SHA256
```

不要只写“全绿”。

---

## 9. 测试质量要求

独立审查指出以下旧测试“名称强于覆盖”：translation ordering、segment crash
recovery、summary atomic write、WebSocket replay，以及旧 soak restart/backlog。

新测试必须具备故障注入或精确竞态控制，而不是顺着实现跑成功路径。

每个修复报告都必须给出：

1. 测试文件与测试名；
2. 注入的具体 bug/crash/race；
3. 预期不变量；
4. 若故意重新引入原 bug，哪个 assertion 会红；
5. 测试是否使用 production construction path；
6. 是否有 mock；若有，mock 证明什么、不能证明什么。

尤其不要：

- 通过私有字段直接设置正确结果后再断言自己设置的结果；
- 从当前实现行为反抄 expected values；
- 用 quiescent WebSocket replay 冒充 replay/live race；
- 用所有 chunks 预先写好后一次性 drain 冒充 mid-stream restart；
- 接受 `SUMMARY_SAVED` 或 `RECOVERY_REQUIRED` 两种结果来掩盖恢复失败；
- 删除、跳过或放宽独立审查暴露 bug 的断言。

---

## 10. 真实验收边界

目前可以作为历史证据的仅有：

- 30 分钟真实麦克风 capture（旧 Phase 2，特定日期/机器快照）；
- 多轮 deterministic backend tests；
- 前端 typecheck/build；
- 合成/加速音频测试。

目前仍未验证：

- 真实 MiMo ASR、翻译和 summary 的权限、配额、成本、质量、延迟、超时与限流；
- 真实三小时挂钟麦克风；
- macOS 麦克风首次拒绝、运行中撤销、重新授权；
- 输入设备丢失/切换/重连；
- sleep/wake、合盖、跨 boot monotonic 行为的完整硬件矩阵；
- 真实课堂内容的最终 purge；
- 独立 Oracle 对英文事实、中文翻译和十节总结质量的判断。

没有用户现场授权时，不要替用户修改隐私权限、让机器睡眠、使用真实麦克风、
发送真实课堂内容到 MiMo 或删除真实数据。只保留 `NOT TESTED` 门。

---

## 11. 实施顺序

按此顺序工作：

### A. 保护和复现

- 保存当前 status/diff/log；
- 做 pre-Claude 快照；
- 运行当前第 3 项 focused tests，确认已知 worker wiring failure；
- 不先运行旧 soak，因为它的证据模型已知无效。

### B. 完成阻塞项 3

- 修 production `create_app` worker-count wiring；
- 清理/加强 worker wiring test；
- 审计 direct translation commit bypass；
- 运行 translation/context/concurrency focused tests；
- 运行一次完整后端 suite；
- 记录计数和 warning。

### C. 快速复核候选项 1–2、4–9

- 读代码，不只跑现有测试；
- 逐个运行 adversarial focused tests；
- 不要顺手扩展无关功能；
- 若发现新问题，记录到新报告的 `NEW FINDINGS`，只有它直接阻断 10 项修复时才
  在本轮修改。

### D. 重写并验证阻塞项 10

- 新写可信 smoke；
- 故意做一次 mutation/probe，确认移除 live Segmenter 或 Sequencer 会让测试红；
- 跑 smoke；
- 跑 fresh 9000-second-equivalent soak；
- 保存 raw artifacts、hash 和新报告；
- 再跑完整 backend + frontend。

### E. 交回 Codex

- 不修改独立审查原报告；
- 可更新 `REPAIR_REPORT.md` 中明确过时的当前状态，但必须保留历史事实；更推荐
  新建 `reports/CLAUDE_CODE_TAKEOVER_REPORT.md`；
- 报告 current HEAD、工作树、所有命令、结果、warnings、artifact paths；
- 列出 10 项每项的候选证据与仍未验证边界；
- 明确写“等待 Codex 独立复审，不自行宣布通过”。

---

## 12. 交付报告模板

请生成：

```text
reports/CLAUDE_CODE_TAKEOVER_REPORT.md
```

至少包含：

```markdown
# Claude Code Takeover / Repair Report

## Repository protection
- takeover baseline commit
- initial/final git status
- preserved pre-existing changes

## Current verification
- backend full suite
- frontend typecheck/build
- migrations
- secret scan

## Blocking issues 1–10
### Issue N
- prior finding
- actual code change or verification
- focused test path and name
- injected bug/race/crash
- mutation that would make it fail
- command/result
- status: repair candidate / still open

## Fresh soak
- exact command and commit
- synthetic audio duration
- actual wall-clock duration
- producer/segmenter/ASR/translation/sequencer overlap evidence
- durable cursor and backlog before restart
- restart/recovery evidence
- counts, duplicates, loss, residue
- RSS/FD/SQLite integrity
- faults/retries
- artifact paths and SHA256

## New findings
- only issues discovered outside the original ten

## Still NOT TESTED
- real MiMo
- real 3-hour microphone
- permissions/device/sleep-wake
- real-data purge
- Oracle quality

## Handoff
10 项 BLOCKING ISSUES 已形成修复候选和证据，等待 Codex 独立复审，不自行宣布通过。
```

如果任何一项未完成，不得使用最后一句“10 项已形成修复候选”；应明确写仍开放的
编号、原因和下一步。

---

## 13. 当前最重要的事实摘要

在你开始之前，请复述并遵守以下事实：

1. 项目不是从零开始，不能重写架构或删除现有修复；
2. `ee72af2` 是独立审查前快照，审查结论 FAIL；
3. `4948567` 是部分 repair 快照，不是验收提交；
4. 当前仍有 4 个未提交路径，必须先保护；
5. 阻塞项 1–2、4–9 有候选实现，但尚未由 Codex 复审；
6. 阻塞项 3 当前真实测试仍红：production `create_app` 没有传 worker counts；
7. 阻塞项 10 尚未修复：旧 soak 没有在生产 loop 跑 Segmenter，也没有
   TranslationSequencer；
8. 旧的 173 tests、184 tests 和 1800 segments 都是历史快照，不能冒充当前结果；
9. 合成/加速 soak 不能冒充真实三小时麦克风；
10. 你可以完成实现和证据，但不能自行宣布 PASS，最终交回 Codex Gatekeeper。

开始工作时，先给出初始 status/diff 的事实摘要，再创建回退快照，然后从阻塞项 3
继续。不要询问无必要问题；遇到真实 MiMo、真实麦克风、隐私权限、睡眠/设备动作
或真实数据删除才停下来请求用户明确授权。

