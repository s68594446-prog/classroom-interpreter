# Classroom Interpreter

面向 macOS 的本地课堂转录与翻译工具：采集音频、保存原始片段、调用配置的 MiMo 服务，并展示英文转录与中文翻译。数据优先落盘；没有有效总结不得清理原始材料。

## 当前状态

这是持续开发中的项目，尚未完成独立的最终验收。历史验证包括：

- [2026-09-11 短时真实 MiMo 验证](reports/LIVE_MIMO_VERIFICATION_2026-09-11.md)。
- [2026-09-12 小规模课堂素材测试](reports/REAL_CLASSROOM_TEST_2026-09-12.md)。
- [2026-09-12 持续运行记录](reports/SUSTAINED_RUN_2026-09-12.md)：原计划 60 分钟，约 29 分钟停止；之后的修复不能视为已完成同等端到端复测。

三小时运行、休眠与设备/权限恢复、真实数据清理以及独立最终验收仍需专项验证。离线测试通过只证明测试覆盖的行为。

## 安装与运行

建议 macOS、Python 3.13、Node.js 24。以下命令从仓库根目录运行：

```sh
git clone https://github.com/s68594446-prog/classroom-interpreter.git
cd classroom-interpreter
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,audio]"
(cd frontend && npm ci && npm run build)
cp .env.example .env
```

在本地 `.env` 配置服务密钥，不要提交密钥。启动服务：

```sh
.venv/bin/python -m uvicorn classroom_interpreter.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。开始课堂前检查输入设备与 macOS 麦克风权限；构建和普通测试不会启动真实录音。启用提供商后，音频与文本会发送到配置的外部服务。默认运行数据保存在 `~/Library/Application Support/Classroom Interpreter`。

## 离线验证

```sh
(cd frontend && npm run typecheck && npm run build)
.venv/bin/python -m pytest
```

测试使用合成输入和模拟服务，不需要生产密钥。真实麦克风、收费模型、长时间运行和清理验收必须单独安排，不能用离线测试替代。

## 开发边界

阅读 [AGENTS.md](AGENTS.md) 和 [CONTRIBUTING.md](CONTRIBUTING.md) 后再修改。保留 `PERSIST FIRST, PROCESS SECOND` 与 `NO VALID SUMMARY = NO PURGE` 两项约束。不要把真实课堂录音、转录或本机配置作为新增测试素材。

本项目采用 [MIT License](LICENSE)。安全问题请见 [SECURITY.md](SECURITY.md)。
