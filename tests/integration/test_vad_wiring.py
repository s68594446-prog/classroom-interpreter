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
