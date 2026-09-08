from pathlib import Path

import pytest
from pydantic import ValidationError

from classroom_interpreter.config import Settings


def test_defaults_are_loopback_and_english(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        classroom_interpreter_data_dir=tmp_path,
    )
    assert settings.app_host == "127.0.0.1"
    assert settings.mimo_asr_language == "en"
    assert settings.database_path == tmp_path / "runtime.sqlite3"


def test_external_bind_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="127.0.0.1"):
        Settings(
            _env_file=None,
            app_host="0.0.0.0",
            classroom_interpreter_data_dir=tmp_path,
        )


def test_non_english_asr_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be en"):
        Settings(
            _env_file=None,
            mimo_asr_language="auto",
            classroom_interpreter_data_dir=tmp_path,
        )


def test_segment_duration_order_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="min <= preferred <= max"):
        Settings(
            _env_file=None,
            segment_min_seconds=10,
            segment_preferred_seconds=5,
            segment_max_seconds=20,
            classroom_interpreter_data_dir=tmp_path,
        )


@pytest.mark.parametrize("unsafe_path", [Path("/"), Path.home()])
def test_broad_runtime_data_directories_are_rejected(unsafe_path: Path) -> None:
    with pytest.raises(ValidationError, match="runtime data directory"):
        Settings(
            _env_file=None,
            classroom_interpreter_data_dir=unsafe_path,
        )
