"""Fixes driven by a real classroom recording, 2026-09-12.

Everything here comes from running the pipeline against an actual lecture
played through speakers and picked up by the laptop microphone -- the first
time this project saw real accented human speech rather than `say` output.
Three problems appeared that synthesized audio had never produced.
"""

from __future__ import annotations

import pytest

from classroom_interpreter.agents.translation import (
    CHINESE_PASSTHROUGH_RATIO,
    is_predominantly_chinese,
)
from classroom_interpreter.config import Settings
from classroom_interpreter.runtime.pipeline import _MIN_PARTIAL_WORDS


# --- Finding 1: lecturers code-switch -------------------------------------
#
# One segment's "English" transcript came back as
# "呃，至少是一千一百亿。China is a huge market." -- the lecturer was speaking
# Chinese. Sending that to a translator asks it to translate Chinese into
# Chinese, which burns a request and returns a paraphrase less faithful than
# what was actually said.

@pytest.mark.parametrize(
    "text",
    [
        "呃，至少是1100亿。",
        "这个市场非常大。",
        "所以我们看到的其实是增长。",
    ],
)
def test_chinese_transcripts_skip_translation(text):
    """Mutation that turns this red: always call the provider."""
    assert is_predominantly_chinese(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "The agent commission, and then we have to pay the other expenses.",
        # Code-switching mid-sentence still has English to translate, so it
        # must NOT be passed through -- the reader would get no Chinese at all.
        "呃，至少是一千一百亿。China is a huge market.",
        # Borrowed terms are normal in a bilingual lecture.
        "We discussed 微积分 today, and the 期权 pricing model.",
        "",
    ],
)
def test_english_and_mixed_transcripts_still_get_translated(text):
    """The dangerous failure is passing through text that needed translating.

    A segment dropped to passthrough shows the reader English where Chinese
    was expected. This is why the threshold is a clear majority rather than
    "contains any Chinese".
    """
    assert is_predominantly_chinese(text) is False


def test_digits_and_punctuation_do_not_dilute_the_ratio():
    """"至少是1100亿。" is Chinese even though half its characters are not."""
    assert is_predominantly_chinese("至少是1100亿。") is True
    assert CHINESE_PASSTHROUGH_RATIO == 0.5


# --- Finding 2: interim transcripts need more audio than I assumed ---------
#
# Measured on the same recording, transcribing the opening of one utterance:
#   1.2s -> "8."                      (garbage)
#   1.5s -> "Age."                    (garbage)
#   2.0s -> "The agents coming."      (nearly)
#   2.5s -> "The agent commission."   (correct)
# The original 1200ms floor therefore published confident nonsense.

def test_partial_audio_floor_is_above_the_garbage_threshold(tmp_path):
    """Mutation that turns this red: lower the floor back toward 1200ms."""
    settings = Settings(
        _env_file=None, classroom_interpreter_data_dir=tmp_path / "d"
    )
    assert settings.partial_transcription_min_audio_ms >= 2500


def test_single_word_partials_are_suppressed():
    """A one-token interim reads as fact and is not one.

    "8." and "Age." were both real outputs. Requiring at least two words
    costs a fraction of a second and removes the whole class of them.
    """
    assert _MIN_PARTIAL_WORDS >= 2
    assert len("8.".split()) < _MIN_PARTIAL_WORDS
    assert len("Age.".split()) < _MIN_PARTIAL_WORDS
    assert len("The agent commission.".split()) >= _MIN_PARTIAL_WORDS
