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

interface GlossaryTerm {
  id: string;
  term: string;
  preferred_chinese: string | null;
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
  const [autoPausedAfter, setAutoPausedAfter] = useState<number | null>(null);
  const [terms, setTerms] = useState<GlossaryTerm[]>([]);
  const [termInput, setTermInput] = useState("");
  const [termZh, setTermZh] = useState("");
  const [glossaryOpen, setGlossaryOpen] = useState(false);
  // The sentence still being spoken. `chineseFor` records which English the
  // Chinese was translated from, so a stale Chinese line is dropped the
  // moment the transcriber revises the words underneath it.
  const [partial, setPartial] = useState<{
    english: string;
    chinese: string;
    chineseFor: string;
  }>({ english: "", chinese: "", chineseFor: "" });

  const cursorRef = useRef(-1);
  const streamEndRef = useRef<HTMLDivElement | null>(null);
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
        if (event.kind === "subtitle.partial") {
          // Interim text for the sentence still being spoken. Not sequenced,
          // so it never advances the replay cursor; superseded the moment the
          // segment seals.
          const english = String(payload.english_text ?? "");
          const chinese = payload.chinese_text
            ? String(payload.chinese_text)
            : undefined;
          setPartial((previous) => {
            if (chinese !== undefined) {
              return { english, chinese, chineseFor: english };
            }
            // English arrives first and alone, so the Chinese here belongs to
            // a shorter prefix of the same sentence. Keep it while it is still
            // a prefix -- a translation of the first half is right, just
            // incomplete -- and drop it the moment the transcriber revises
            // what it heard, because then it is simply wrong.
            const stillValid =
              previous.chineseFor !== "" && english.startsWith(previous.chineseFor);
            return {
              english,
              chinese: stillValid ? previous.chinese : "",
              chineseFor: stillValid ? previous.chineseFor : "",
            };
          });
          return;
        }
        if (event.kind === "subtitle.current") {
          setPartial({ english: "", chinese: "", chineseFor: "" });
          setCurrent({
            sequence: Number(payload.sequence),
            english_text: String(payload.english_text ?? ""),
          });
        } else if (event.kind === "subtitle.committed") {
          setPartial({ english: "", chinese: "", chineseFor: "" });
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
          const reason = payload.auto_paused_after_seconds;
          setAutoPausedAfter(typeof reason === "number" ? reason : null);
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
      setPartial({ english: "", chinese: "", chineseFor: "" });
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
        setError(body.detail ?? "开始上课失败");
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
    setError(null);
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

  const loadGlossary = useCallback(async (course: string) => {
    if (!course.trim()) return;
    const r = await fetch(`/api/glossary/${encodeURIComponent(course)}`);
    if (r.ok) setTerms(((await r.json()).terms ?? []) as GlossaryTerm[]);
  }, []);

  const addTerm = async (course: string) => {
    const term = termInput.trim();
    if (!term) return;
    const r = await fetch(`/api/glossary/${encodeURIComponent(course)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        term,
        preferred_chinese: termZh.trim() || null,
      }),
    });
    if (r.ok) {
      setTermInput("");
      setTermZh("");
      await loadGlossary(course);
    } else {
      setError("术语添加失败");
    }
  };

  const removeTerm = async (id: string, course: string) => {
    const r = await fetch(`/api/glossary/term/${id}`, { method: "DELETE" });
    if (r.ok) await loadGlossary(course);
  };

  const resumeRecovery = async (sessionId: string) => {
    // Clear the previous failure first. Without this a stale banner (e.g.
    // SINGLE_ACTIVE_SESSION from an earlier Start Class) stays on screen after
    // this action succeeds, and reads as if *this* action failed.
    setError(null);
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
    setError(null);
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

  // Keep the newest caption in view, but never yank the page while the user is
  // scrolled up reading something earlier.
  useEffect(() => {
    const node = streamEndRef.current;
    if (!node) return;
    const stream = node.parentElement;
    if (stream) {
      const distanceFromBottom =
        stream.scrollHeight - stream.scrollTop - stream.clientHeight;
      if (distanceFromBottom > 160) return;
    }
    node.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [committed.length, current?.sequence, partial.english, partial.chinese]);

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
              {starting ? "正在启动…" : "开始上课"}
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
            {paused && autoPausedAfter !== null && (
              <p className="auto-pause-banner" role="status">
                已自动暂停：检测到 {Math.round(autoPausedAfter)} 秒无人说话。
                已录制的内容都在，按「继续」恢复。
              </p>
            )}
            {/* A microphone that stops is the worst thing that can happen here
                and it used to be completely invisible: capture died 23 minutes
                into a 29-minute run, the status stayed RECORDING, health stayed
                green, and nothing said a word until the class was ended. This
                has to be loud, and it has to be on the recording screen -- by
                the time someone reaches the recovery page the class is over. */}
            {(session.first_error_category === "CAPTURE" ||
              session.last_error_category === "CAPTURE") && (
              <p className="capture-fault-banner" role="alert">
                ⚠️ 录音已中断，之后的声音<strong>没有被录下来</strong>。
                {session.first_error_safe_message ??
                session.last_error_safe_message
                  ? `（${
                      session.first_error_category === "CAPTURE"
                        ? session.first_error_safe_message
                        : session.last_error_safe_message
                    }）`
                  : ""}
                请检查麦克风是否被其他程序占用或被拔出，然后结束本节课并重新开始。
                已经录下的内容都还在。
              </p>
            )}
            <div className="controls">
              <button
                type="button"
                onClick={() => control("pause")}
                disabled={!recording}
              >
                暂停
              </button>
              <button
                type="button"
                onClick={() => control("resume")}
                disabled={!paused}
              >
                继续
              </button>
              <button
                type="button"
                onClick={() => control("end")}
                disabled={!recording && !paused}
              >
                {endBusy ? "正在收尾…" : "结束上课"}
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
                {/* The reason existed in `capture_events` and the audit table
                    the whole time, neither of which anyone can see. Showing the
                    category alone still left "处理未能全部完成" as the entire
                    explanation for a class that died of a named fault. */}
                {/* The FIRST error, not the last. A capture fault stops the
                    recording; ending the class then fails the summary because
                    the transcript is incomplete, and that failure used to
                    overwrite the fault that caused it -- so the explanation on
                    screen was "summary failed", which is true and useless. */}
                {(session.first_error_safe_message ??
                  session.last_error_safe_message) && (
                  <p className="recovery-reason">
                    原因：
                    {session.first_error_safe_message ??
                      session.last_error_safe_message}
                    {session.first_error_safe_message &&
                    session.last_error_safe_message &&
                    session.last_error_safe_message !==
                      session.first_error_safe_message
                      ? `，随后：${session.last_error_safe_message}`
                      : ""}
                  </p>
                )}
                <p>
                  处理未能全部完成
                  {session.first_error_category ?? session.last_error_category
                    ? `（${
                        session.first_error_category ??
                        session.last_error_category
                      }）`
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

          <section className="glossary-card">
            <button
              type="button"
              className="glossary-toggle"
              onClick={() => {
                const next = !glossaryOpen;
                setGlossaryOpen(next);
                if (next) loadGlossary(session.course_name);
              }}
              aria-expanded={glossaryOpen}
            >
              <span>术语库</span>
              <span className="glossary-count">
                {terms.length > 0 ? `${terms.length} 个` : "提高专业词准确率"}
              </span>
            </button>
            {glossaryOpen && (
              <div className="glossary-body">
                <p className="glossary-help">
                  加入本课的专业术语，识别与翻译会优先使用它们；填了中文就会固定用该译法，
                  避免同一个词每段翻得不一样。
                </p>
                <div className="glossary-input-row">
                  <input
                    value={termInput}
                    onChange={(e) => setTermInput(e.target.value)}
                    placeholder="英文术语，如 Ito's lemma"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addTerm(session.course_name);
                    }}
                  />
                  <input
                    value={termZh}
                    onChange={(e) => setTermZh(e.target.value)}
                    placeholder="指定中文（可留空）"
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addTerm(session.course_name);
                    }}
                  />
                  <button type="button" onClick={() => addTerm(session.course_name)}>
                    添加
                  </button>
                </div>
                <ul className="glossary-list">
                  {terms.length === 0 && <li className="glossary-empty">还没有术语</li>}
                  {terms.map((t) => (
                    <li key={t.id}>
                      <span className="glossary-term">{t.term}</span>
                      {t.preferred_chinese && (
                        <span className="glossary-zh">→ {t.preferred_chinese}</span>
                      )}
                      <button
                        type="button"
                        className="glossary-remove"
                        aria-label={`删除 ${t.term}`}
                        onClick={() => removeTerm(t.id, session.course_name)}
                      >
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          <section className="caption-stream" aria-label="字幕" aria-live="polite">
            {committed.length === 0 && !current && (
              <p className="stream-empty">
                等待第一句…（英文先出现，中文随后补上）
              </p>
            )}

            {committed.map((item) => (
              <article className="caption" key={item.sequence}>
                <span className="caption-seq">{item.sequence + 1}</span>
                <div className="caption-body">
                  {/* Chinese first and largest: it is what the reader is here
                      for. English stays beneath as the checkable source. */}
                  <p className="caption-zh">{item.chinese_text}</p>
                  <p className="caption-en">{item.english_text}</p>
                </div>
              </article>
            ))}

            {/* English lands ~2.3s before Chinese. Showing it immediately, in
                the same stream, is the difference between a ~6s and a ~3.6s
                perceived delay -- the row is already on screen and fills in. */}
            {current && (
              <article className="caption caption-pending" key={`p-${current.sequence}`}>
                <span className="caption-seq">{current.sequence + 1}</span>
                <div className="caption-body">
                  <p className="caption-zh caption-waiting">
                    <span className="dot" />
                    <span className="dot" />
                    <span className="dot" />
                    <span className="waiting-label">翻译中</span>
                  </p>
                  <p className="caption-en">{current.english_text}</p>
                </div>
              </article>
            )}
            {/* The sentence still being spoken -- the live line. Appears ~2s
                behind the speaker instead of waiting for the segment to seal,
                and the Chinese fills in beneath it a moment later.

                Not gated on `current` any more: while a sealed sentence waits
                for its translation the speaker is already well into the next
                one, and hiding the live line for that window made the thing
                stutter exactly when it should feel continuous. Rendered in
                dimmed italics and marked 正在识别 so it is never mistaken for
                the committed record. */}
            {partial.english && (
              <article className="caption caption-interim" aria-label="正在识别">
                <span className="caption-seq">·</span>
                <div className="caption-body">
                  {partial.chinese && (
                    <p className="caption-zh caption-interim-text">
                      {partial.chinese}
                    </p>
                  )}
                  <p className="caption-en caption-interim-text">
                    {partial.english}
                  </p>
                </div>
              </article>
            )}
            <div ref={streamEndRef} />
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
