export type HealthStatus = "green" | "yellow" | "red" | "unknown";

export interface HealthComponent {
  name: string;
  status: HealthStatus;
  reason_code: string;
  checked_at: string;
}

export interface HealthSnapshot {
  status: HealthStatus;
  phase: string;
  app_version: string;
  components: HealthComponent[];
  checked_at: string;
}

export type WebSocketEventKind =
  | "system.ready"
  | "system.pong"
  | "session.state"
  | "subtitle.current"
  // Interim transcript of the sentence still being spoken. Ephemeral: carries
  // event_sequence -1 and is never persisted, so it must not advance the
  // replay cursor.
  | "subtitle.partial"
  | "subtitle.committed"
  | "health.snapshot";

export interface WebSocketEvent {
  schema_version: "1";
  event_sequence: number;
  kind: WebSocketEventKind;
  session_id: string | null;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export type SessionStatus =
  | "NEW"
  | "RECORDING"
  | "PAUSED"
  | "FINALIZING"
  | "SUMMARIZING"
  | "SUMMARY_SAVED"
  | "PURGING"
  | "PURGED"
  | "RECOVERY_REQUIRED";

export interface SessionRecord {
  id: string;
  course_name: string;
  status: SessionStatus;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  summary_path: string | null;
  summary_hash: string | null;
  summary_model: string | null;
  app_version: string;
  next_translation_sequence: number;
  last_error_category?: string | null;
  last_error_safe_message?: string | null;
  // The first error of the session. The last one is usually its consequence:
  // a capture fault stops the recording, and the summary failure that follows
  // overwrites it.
  first_error_category?: string | null;
  first_error_safe_message?: string | null;
}

export interface Progress {
  segment_counts: Record<string, number>;
  pending_segments: number;
  failed_segments: number;
  captured_audio_ms: number;
  processed_audio_ms: number;
  live_delay_ms: number;
  capture_chunks: number;
}

export interface CommittedItem {
  sequence: number;
  english_text: string;
  chinese_text: string;
  start_ms: number;
  end_ms: number;
}

export interface SessionView {
  session: SessionRecord;
  progress: Progress;
  committed_history?: CommittedItem[];
}

export interface RecoverySession {
  session: SessionRecord;
  progress: Progress;
}
