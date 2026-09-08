import { useCallback, useEffect, useRef, useState } from "react";

import type {
  CommittedItem,
  HealthSnapshot,
  Progress,
  SessionRecord,
  SessionStatus,
  WebSocketEvent,
} from "./contracts";
import "./styles.css";

const POLL_MS = 3000;

interface CurrentSubtitle {
  sequence: number;
  english_text: string;
}

function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function statusLabel(status: SessionStatus): string {
  switch (status) {
    case "RECORDING":
      return "LIVE";
    case "PAUSED":
      return "PAUSED";
    case "FINALIZING":
      return "FINALIZING";
    case "SUMMARIZING":
      return "SUMMARY";
    case "SUMMARY_SAVED":
      return "SAVED";
    case "RECOVERY_REQUIRED":
      return "RECOVERY REQUIRED";
    case "PURGING":
      return "PURGING";
    case "PURGED":
      return "PURGED";
    default:
      return status;
  }
}

export function App() {
  const [session, setSession] = useState<SessionRecord | null>(null);
  const [progress, setProgress] = useState<Progress | null>(null);
  const [committed, setCommitted] = useState<CommittedItem[]>([]);
  const [current, setCurrent] = useState<CurrentSubtitle | null>(null);
  const [health, setHealth] = useState<HealthSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [courseName, setCourseName] = useState("");
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recoverable, setRecoverable] = useState<SessionRecord[]>([]);
  const [now, setNow] = useState(Date.now());
  const [endBusy, setEndBusy] = useState(false);

  const cursorRef = useRef(-1);
  const wsRef = useRef<WebSocket | null>(null);
  const sessionIdRef = useRef<string | null>(null);

  const applyCommitted = useCallback((item: CommittedItem) => {
    setCommitted((previous) => {
      const without = previous.filter((c) => c.sequence !== item.sequence);
      return [...without, item].sort((a, b) => a.sequence - b.sequence);
    });
  }, []);

  const connectWs = useCallback(
    (sessionId: string) => {
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          /* ignore */
        }
      }
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const socket = new WebSocket(
        `${protocol}//${location.host}/ws/sessions/${sessionId}?cursor=${cursorRef.current}`,
      );
      wsRef.current = socket;
      socket.onopen = () => setConnected(true);
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data) as WebSocketEvent;
        if (event.kind === "system.pong") return;
        if (event.event_sequence > cursorRef.current) {
          cursorRef.current = event.event_sequence;
        }
        const payload = event.payload as Record<string, unknown>;
        if (event.kind === "subtitle.current") {
          setCurrent({
            sequence: Number(payload.sequence),
            english_text: String(payload.english_text ?? ""),
          });
        } else if (event.kind === "subtitle.committed") {
          applyCommitted({
            sequence: Number(payload.sequence),
            english_text: String(payload.english_text ?? ""),
            chinese_text: String(payload.chinese_text ?? ""),
            start_ms: 0,
            end_ms: 0,
          });
          setCurrent((c) =>
            c && c.sequence === Number(payload.sequence) ? null : c,
          );
        } else if (event.kind === "session.state") {
          setSession((s) =>
            s
              ? {
                  ...s,
                  status: String(payload.status) as SessionStatus,
                }
              : s,
          );
          setProgress((payload.progress as Progress) ?? null);
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (sessionIdRef.current === sessionId) {
          setTimeout(() => connectWs(sessionId), 1000);
        }
      };
    },
    [applyCommitted],
  );

  const openSession = useCallback(
    async (sessionId: string) => {
      const response = await fetch(`/api/sessions/${sessionId}`);
      if (!response.ok) {
        setError("无法加载会话");
        return;
      }
      const view = await response.json();
      sessionIdRef.current = sessionId;
      setSession(view.session);
      setProgress(view.progress);
      setCommitted(view.committed_history ?? []);
      setCurrent(null);
      cursorRef.current = -1;
      connectWs(sessionId);
    },
    [connectWs],
  );

  useEffect(() => {
    fetch("/api/recovery")
      .then((r) => (r.ok ? r.json() : { sessions: [] }))
      .then((data) => {
        const sessions = (data.sessions ?? []) as SessionRecord[];
        setRecoverable(sessions.filter((s) => s.status !== "PURGED"));
      })
      .catch(() => setRecoverable([]));
  }, []);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!session) return;
    const id = setInterval(() => {
      fetch("/api/sessions/" + session.id)
        .then((r) => (r.ok ? r.json() : null))
        .then((view) => {
          if (!view) return;
          setSession((s) => (s ? { ...s, ...view.session } : s));
          setProgress(view.progress);
          if (view.committed_history) {
            setCommitted(view.committed_history);
          }
        })
        .catch(() => undefined);
    }, POLL_MS);
    return () => clearInterval(id);
  }, [session]);

  useEffect(() => {
    const id = setInterval(() => {
      fetch("/health")
        .then((r) => (r.ok ? r.json() : null))
        .then((snapshot) => snapshot && setHealth(snapshot))
        .catch(() => undefined);
    }, POLL_MS);
    fetch("/health")
      .then((r) => (r.ok ? r.json() : null))
      .then((snapshot) => snapshot && setHealth(snapshot))
      .catch(() => undefined);
    return () => clearInterval(id);
  }, []);

  const startClass = async () => {
    setStarting(true);
    setError(null);
    try {
      const response = await fetch("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ course_name: courseName.trim() || "未命名课程" }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setError(body.detail ?? "Start Class 失败");
        return;
      }
      const view = await response.json();
      setRecoverable([]);
      await openSession(view.session.id);
    } finally {
      setStarting(false);
    }
  };

  const control = async (action: "pause" | "resume" | "end") => {
    if (!session) return;
    if (action === "end") setEndBusy(true);
    try {
      const response = await fetch(`/api/sessions/${session.id}/${action}`, {
        method: "POST",
      });
      if (response.ok) {
        const record = await response.json();
        setSession((s) => (s ? { ...s, ...record } : s));
      } else {
        const body = await response.json().catch(() => ({}));
        setError(body.detail ?? `${action} 失败`);
      }
    } finally {
      setEndBusy(false);
    }
  };

  const resumeRecovery = async (sessionId: string) => {
    const response = await fetch(`/api/recovery/${sessionId}/resume`, {
      method: "POST",
    });
    if (response.ok) {
      await openSession(sessionId);
    } else {
      const body = await response.json().catch(() => ({}));
      setError(body.detail ?? "恢复失败");
    }
  };

  const deleteRecovery = async (sessionId: string) => {
    const response = await fetch(`/api/recovery/${sessionId}`, {
      method: "DELETE",
    });
    if (response.ok) {
      setRecoverable((list) => list.filter((s) => s.id !== sessionId));
      await fetch("/api/recovery")
        .then((r) => (r.ok ? r.json() : { sessions: [] }))
        .then((data) =>
          setRecoverable(
            ((data.sessions ?? []) as SessionRecord[]).filter(
              (s) => s.status !== "PURGED",
            ),
          ),
        )
        .catch(() => undefined);
    } else {
      const body = await response.json().catch(() => ({}));
      setError(body.detail ?? "删除失败");
    }
  };

  const elapsedMs = (() => {
    if (!session?.started_at) return 0;
    const started = new Date(session.started_at).getTime();
    const ended = session.ended_at ? new Date(session.ended_at).getTime() : now;
    return ended - started;
  })();

  const active = session !== null;
  const recording = session?.status === "RECORDING";
  const paused = session?.status === "PAUSED";
  const finalizing =
    session?.status === "FINALIZING" || session?.status === "SUMMARIZING";
  const recovery = session?.status === "RECOVERY_REQUIRED";
  const backlogSeconds = progress ? progress.live_delay_ms / 1000 : 0;

  return (
    <main className="app-shell">
      <header>
        <p className="eyebrow">CLASSROOM INTERPRETER</p>
        <h1>课堂实时英译中</h1>
        <p className="phase">
          {session ? session.course_name : "本地运行 · 音频发往 MiMo 云端处理"}
        </p>
      </header>

      {!active && (
        <section className="session-card" aria-label="Start Class">
          <label htmlFor="course-name">课程名称</label>
          <input
            id="course-name"
            value={courseName}
            onChange={(e) => setCourseName(e.target.value)}
            placeholder="例如：Risk Management"
            disabled={starting}
          />
          <div className="session-status">
            <button type="button" onClick={startClass} disabled={starting}>
              {starting ? "Starting…" : "Start Class"}
            </button>
          </div>
          {error && <p className="error-text">{error}</p>}
          {recoverable.length > 0 && (
            <div className="recovery-card">
              <h2>未完成的会话</h2>
              <p>以下会话未完成总结与清理，可继续处理或稍后删除。</p>
              {recoverable.map((s) => (
                <div className="recovery-row" key={s.id}>
                  <span>
                    {s.course_name} · {statusLabel(s.status)}
                  </span>
                  <button type="button" onClick={() => resumeRecovery(s.id)}>
                    继续完成
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      if (
                        window.confirm(
                          `确定删除「${s.course_name}」吗？该会话的全部录音、文字与未保存的总结将永久丢失，且无法恢复。`,
                        )
                      ) {
                        deleteRecovery(s.id);
                      }
                    }}
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {active && session && (
        <>
          <section className="session-card" aria-label="Session">
            <div className="session-status">
              <span
                className={`status-dot status-${recovery ? "red" : recording ? "green" : paused ? "yellow" : "unknown"}`}
                aria-hidden="true"
              />
              <span>{statusLabel(session.status)}</span>
              <time>{formatClock(elapsedMs)}</time>
              <span className={connected ? "ws-ok" : "ws-down"}>
                {connected ? "已连接" : "重连中…"}
              </span>
            </div>
            <div className="controls">
              <button
                type="button"
                onClick={() => control("pause")}
                disabled={!recording}
              >
                Pause
              </button>
              <button
                type="button"
                onClick={() => control("resume")}
                disabled={!paused}
              >
                Resume
              </button>
              <button
                type="button"
                onClick={() => control("end")}
                disabled={!recording && !paused}
              >
                {endBusy ? "Draining…" : "End Class"}
              </button>
            </div>
            {progress && (
              <p className="progress-line">
                {recording && backlogSeconds > 3 && (
                  <span className="delay-warning">
                    翻译落后 {backlogSeconds.toFixed(0)} 秒 · 待处理{" "}
                    {progress.pending_segments} 段 · 已录音频不会丢失
                  </span>
                )}
                {!recording && !paused && (
                  <span>
                    已处理 {progress.processed_audio_ms / 1000} 秒音频 / 共{" "}
                    {progress.captured_audio_ms / 1000} 秒
                  </span>
                )}
              </p>
            )}
            {error && <p className="error-text">{error}</p>}
            {recovery && (
              <div className="recovery-card">
                <h2>需要恢复</h2>
                <p>
                  处理未能全部完成
                  {session.last_error_category
                    ? `（错误类别：${session.last_error_category}）`
                    : ""}
                  。已录音频与已翻译内容均已保存。回到首页可选择继续完成处理或删除会话。
                </p>
                <button
                  type="button"
                  onClick={() => {
                    setSession(null);
                    sessionIdRef.current = null;
                  }}
                >
                  回到首页
                </button>
              </div>
            )}
            {finalizing && (
              <p className="progress-line">
                正在等待全部语音与翻译完成，随后生成课程总结…
              </p>
            )}
          </section>

          {current && (
            <section className="subtitle-card" aria-label="Current">
              <p className="label">EN · current（未提交）</p>
              <p>{current.english_text}</p>
            </section>
          )}

          <section className="subtitle-card" aria-label="Committed history">
            <p className="label">已提交 · EN + 中文</p>
            {committed.length === 0 && <p>暂无已提交字幕。</p>}
            {committed.map((item) => (
              <div className="subtitle-row" key={item.sequence}>
                <span className="subtitle-index">#{item.sequence}</span>
                <div>
                  <p className="en">{item.english_text}</p>
                  <p className="zh">{item.chinese_text}</p>
                </div>
              </div>
            ))}
          </section>
        </>
      )}

      <section className="health-card" aria-label="Health">
        <h2>Health</h2>
        {!health && <p>Checking backend…</p>}
        {health?.components.map((component) => (
          <div className="health-row" key={component.name}>
            <span className={`status-dot status-${component.status}`} />
            <span>{component.name}</span>
            <span>{component.reason_code}</span>
          </div>
        ))}
      </section>
    </main>
  );
}
