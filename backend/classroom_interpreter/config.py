from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from classroom_interpreter import __version__


def default_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Classroom Interpreter"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Classroom Interpreter"
    app_version: str = __version__
    app_phase: str = "PHASE_1_SKELETON"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    classroom_interpreter_data_dir: Path = Field(default_factory=default_data_dir)
    log_level: str = "INFO"

    mimo_api_key: str | None = None
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_asr_model: str = "mimo-v2.5-asr"
    mimo_translation_model: str = "mimo-v2.5"
    mimo_summary_model: str = "mimo-v2.5-pro"
    mimo_asr_language: str = "en"

    debug_classroom_content: bool = False

    # First-caption latency is dominated by how long we wait for a segment to
    # close, not by the provider. Measured live on 2026-09-11: ASR ~3.6s +
    # translation ~2.3s = ~6s of API time, against a 12s segment wait -- the
    # wait was two thirds of the delay. Preferred is 6s so a natural pause
    # closes a segment sooner; max stays 30s so a lecturer who never pauses
    # still gets bounded output. Streaming was measured and rejected: MiMo's
    # stream produced a *later* first token (p50 3.09s) than a whole
    # non-streamed reply (2.29s), so it buys nothing here (D-043).
    # How much audio is buffered before it becomes durable and therefore
    # VISIBLE to the segmenter. This is the product's dominant latency term,
    # not the model: at the old hard-coded 20s a speaker waited ~10s on average
    # before processing could even begin, and measured end-to-end English
    # caption latency was p50 12.2s while ASR itself took 1.6s. Sealing costs
    # ~1.5ms whatever the length, so short chunks are close to free.
    capture_chunk_seconds: float = Field(default=0.5, ge=0.5, le=30)
    # How often the segmenter looks for newly committed chunks. It used to be a
    # fixed 0.5s sleep, which added up to half a second of pure waiting to
    # every caption -- comparable to the entire capture wait once chunks became
    # short. Sealing and polling are both cheap; the audio is not waiting on
    # either of them.
    segmenter_poll_seconds: float = Field(default=0.1, gt=0, le=5)
    # How often the microphone is checked for having stopped. `check()` only
    # reads the stream flag and a timestamp, so this is cheap; the cost of
    # NOT running it was six minutes of a class recorded to nowhere.
    capture_check_seconds: float = Field(default=1.0, gt=0, le=60)
    # A discontinuity in the audio stream is recorded as a gap and tolerated up
    # to this length. Beyond it the device is treated as broken and capture
    # stops. Previously ANY discontinuity over 100ms was fatal, which meant one
    # scheduling hiccup could end a three-hour lecture -- and did end a
    # 29-minute test run at t+23.5min.
    capture_gap_tolerance_seconds: float = Field(default=5.0, gt=0, le=120)
    # ...and a stream that keeps gapping is broken however small each one is.
    capture_max_tolerated_gaps: int = Field(default=60, ge=0)
    segment_min_seconds: float = Field(default=2, gt=0)
    segment_preferred_seconds: float = Field(default=6, gt=0)
    segment_max_seconds: float = Field(default=30, gt=0)
    segment_silence_milliseconds: int = Field(default=700, gt=0)
    # How little silence is enough once a segment has already reached its
    # preferred length. Without this, `segment_preferred_seconds` had no
    # effect at all -- it was validated here and never passed to the segmenter.
    segment_preferred_silence_milliseconds: int = Field(default=300, gt=0)
    segment_overlap_milliseconds: int = Field(default=250, ge=0)
    # Auto-pause after a long quiet stretch (break, class ended, mic forgotten).
    # 0 disables it. Must exceed the segment close threshold so an ordinary
    # between-sentence pause can never trip it.
    #
    # Ten minutes, not the 30 s this shipped with. Auto-pause releases the
    # microphone and nothing ever resumes it on its own -- a paused device
    # cannot hear the class start again -- so whatever this fires on, the rest
    # of the lecture is lost unless a person notices and presses 继续.
    #
    # 30 s is a normal event in a classroom: writing on the board, an exercise,
    # a question the microphone does not pick up. Worse, it compounds with the
    # VAD: silence here means "the segmenter heard no speech", so a threshold
    # that drifts too high (which has happened, and cost a live run) stops
    # being a few dropped words and becomes a stopped recording. The feature is
    # meant to catch a microphone left on after everyone leaves, and ten
    # minutes of unbroken silence says that unambiguously.
    #
    # What it costs to wait: ~19 MB of disk per ten minutes of silence. What it
    # saves: the remainder of a class.
    auto_pause_silence_seconds: float = Field(default=600, ge=0)
    # Interim transcription of the sentence still being spoken. 0 disables it.
    # This is what makes a caption appear ~2s after speech instead of ~7s, and
    # it costs extra ASR calls -- roughly one per interval per utterance -- so
    # it is a latency/cost dial, not a free win (D-046).
    partial_transcription_interval_seconds: float = Field(default=2.0, ge=0)
    # 2500ms, not less. Measured on real classroom audio through a microphone:
    # 1.2s of speech transcribed to "8.", 1.5s to "Age.", and only at 2.5s did
    # it become the correct "The agent commission." Showing a wrong fragment
    # 1s sooner is negative value -- the reader trusts it and it is noise.
    partial_transcription_min_audio_ms: int = Field(default=2500, gt=0)
    # Interim TRANSLATION of the sentence still being spoken. 0 disables it.
    #
    # This is what separates a caption from simultaneous interpretation. While
    # Chinese only ever comes from a sealed segment, the first word of a 6.5s
    # sentence waits the whole sentence plus ~3.4s of provider round trip --
    # about 10s -- before any Chinese exists for it. Translating the open
    # utterance puts Chinese on screen a few seconds behind the speaker and
    # keeps correcting it, and the sealed translation still replaces it.
    #
    # It is the most expensive dial in the product: every interval is an extra
    # translation call on top of the extra ASR call partials already cost.
    # 3s rather than matching the 2s transcription interval, and skipped
    # entirely unless the English actually moved (below), to keep that bounded.
    partial_translation_interval_seconds: float = Field(default=3.0, ge=0)
    # Re-translating for one extra word spends money to change nothing the
    # reader would notice.
    partial_translation_min_new_words: int = Field(default=3, ge=1)
    # The absolute floor under the adaptive VAD threshold, not the threshold.
    vad_threshold_rms: float = Field(default=300, gt=0)
    # Speech starts at this multiple of the running noise-floor estimate. The
    # estimate does nothing in a quiet room -- it pins to its minimum and the
    # absolute floor above wins -- and earns its keep in a noisy hall.
    vad_noise_floor_multiplier: float = Field(default=4.0, ge=1.0)
    # How much CONTINUOUS speech cancels a silence run. A single 20ms frame
    # used to, so one keystroke or reverb tail in a quiet gap held the segment
    # open to the 30s cap. Measured live, this single change moved p50 segment
    # duration from 27.2s to 11.4s before the preferred-length close was added.
    vad_voiced_onset_milliseconds: int = Field(default=200, ge=0)
    context_max_segments: int = Field(default=8, gt=0)
    context_max_seconds: int = Field(default=90, gt=0)
    context_max_tokens: int = Field(default=1200, gt=0)

    asr_worker_count: int = Field(default=2, ge=1, le=8)
    asr_lease_seconds: float = Field(default=120, gt=0)
    asr_max_attempts: int = Field(default=5, ge=1)
    asr_backoff_base_seconds: float = Field(default=1, gt=0)
    asr_backoff_cap_seconds: float = Field(default=60, gt=0)
    asr_request_timeout_seconds: float = Field(default=30, gt=0)
    asr_backoff_jitter_fraction: float = Field(default=0.1, ge=0, le=1)

    translation_worker_count: int = Field(default=2, ge=1, le=8)
    translation_lease_seconds: float = Field(default=120, gt=0)
    translation_max_attempts: int = Field(default=5, ge=1)
    translation_backoff_base_seconds: float = Field(default=1, gt=0)
    translation_backoff_cap_seconds: float = Field(default=60, gt=0)
    translation_request_timeout_seconds: float = Field(default=30, gt=0)

    summary_chunk_token_budget: int = Field(default=3000, gt=0)
    summary_max_attempts: int = Field(default=3, ge=1)
    health_flush_interval_seconds: float = Field(default=1.0, gt=0)
    health_buffer_max_samples: int = Field(default=4096, ge=64)

    @field_validator("app_host")
    @classmethod
    def host_must_be_loopback(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("V1 APP_HOST must be exactly 127.0.0.1")
        return value

    @field_validator("mimo_asr_language")
    @classmethod
    def language_must_be_english(cls, value: str) -> str:
        if value != "en":
            raise ValueError("V1 MIMO_ASR_LANGUAGE must be en")
        return value

    @field_validator(
        "mimo_base_url",
        "mimo_asr_model",
        "mimo_translation_model",
        "mimo_summary_model",
    )
    @classmethod
    def provider_values_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider configuration values must not be blank")
        return value.strip()

    @field_validator("classroom_interpreter_data_dir")
    @classmethod
    def data_dir_must_not_be_broad(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if resolved in {Path("/"), Path.home().resolve()}:
            raise ValueError("runtime data directory cannot be / or the home directory")
        return resolved

    @model_validator(mode="after")
    def segment_bounds_must_be_ordered(self) -> Self:
        if not (
            self.segment_min_seconds
            <= self.segment_preferred_seconds
            <= self.segment_max_seconds
        ):
            raise ValueError("segment durations must satisfy min <= preferred <= max")
        if self.segment_overlap_milliseconds >= self.segment_max_seconds * 1000:
            raise ValueError("segment overlap must be shorter than maximum segment duration")
        if (
            self.auto_pause_silence_seconds
            and self.auto_pause_silence_seconds * 1000
            <= self.segment_silence_milliseconds
        ):
            raise ValueError(
                "auto-pause silence must be longer than the segment silence threshold"
            )
        return self

    @property
    def database_path(self) -> Path:
        return self.classroom_interpreter_data_dir.expanduser() / "runtime.sqlite3"

    @property
    def health_database_path(self) -> Path:
        return self.classroom_interpreter_data_dir.expanduser() / "health.sqlite3"


def get_settings() -> Settings:
    return Settings()
