"""Settings the segmenter never receives are settings that do nothing.

`segment_preferred_seconds` was defined in Settings, validated by a Settings
validator, documented in `.env.example`, and never passed to `SegmenterConfig`.
D-043 "lowered segmentation from 12s to 6s" by changing a number that nothing
read, which is why the local `.env` could sit at 12 for days with no observable
difference.
"""

from __future__ import annotations


def test_preferred_length_actually_reaches_the_segmenter(tmp_path):
    """It was configured, validated, and silently discarded.

    Mutation that turns this red: drop `preferred_seconds` from the
    `SegmenterConfig(...)` built in `pipeline.py`.
    """
    from test_live_api import make_client

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    runtime.settings = runtime.settings.model_copy(
        update={"segment_preferred_seconds": 7.0}
    )
    with client:
        client.post("/api/sessions", json={"course_name": "Preferred"})
        configured = runtime._segmenter.config.preferred_seconds
    assert configured == 7.0, (
        "segment_preferred_seconds never reaches the segmenter, so setting it "
        f"has no effect at all (segmenter sees {configured})"
    )


def test_the_input_level_reaches_the_client(tmp_path):
    """An audio product that hears nothing looks exactly like an audio product
    in a quiet room, and this one has failed that way more than once.

    It also makes the empty-room rule diagnosable: that rule silences a window
    whose peak is below `threshold_rms`, so a microphone with its gain turned
    down is indistinguishable from an empty room unless the peak is on screen.

    Levels only -- an RMS peak, an RMS floor, a threshold. No content.

    Mutation that turns this red: drop `input_level` from `progress()`.
    """
    import time

    from test_live_api import make_client

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Level"}).json()
        session_id = view["session"]["id"]
        deadline = time.monotonic() + 10.0
        level = None
        while time.monotonic() < deadline:
            level = client.get(f"/api/sessions/{session_id}").json()["progress"][
                "input_level"
            ]
            if level and level["peak_rms"] > 0:
                break
            time.sleep(0.1)

    assert level is not None, "the client cannot tell whether anything is heard"
    assert level["peak_rms"] > 0, "no level was ever reported for real audio"
    assert level["speech_floor_rms"] > 0
    assert set(level) == {
        "peak_rms",
        "floor_rms",
        "speech_floor_rms",
        "hearing_speech",
    }, "the level payload must carry levels only, never content"
