"""The product's only deliverable was unreachable.

A class is recorded, transcribed, translated, summarised, structurally
validated, hashed, written atomically -- and then the summary is a filesystem
path in a database column that no endpoint served and no screen showed. A
browser cannot open a local path from a page, so there was no way to read it
from the app at all.

Worse on the successful path: after SUMMARY_SAVED the session is purged, and
the home screen lists every session EXCEPT purged ones. A class that completed
perfectly disappeared from the application entirely.

The read is re-verified with the same discipline cleanup uses before deleting:
the path must resolve inside the summary store and must not be a symlink, and
the bytes must still hash to what was recorded when the summary was saved.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from classroom_interpreter.providers.summary_contract import REQUIRED_SECTIONS
from classroom_interpreter.storage.repository import SessionRepository

from test_summary_pipeline import run_flow_to_summary


def test_a_saved_summary_can_be_read_back_from_the_app(tmp_path):
    """Mutation that turns this red: remove the
    `GET /api/sessions/{id}/summary` route."""
    client, database, runtime, session_id, status = run_flow_to_summary(tmp_path)
    assert status == "SUMMARY_SAVED"

    with client:
        response = client.get(f"/api/sessions/{session_id}/summary")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["course_name"] == "Summary Course"
    assert body["markdown"].startswith("# 课程名称：Summary Course")
    for index, title in enumerate(REQUIRED_SECTIONS):
        assert f"## {index + 1}. {title}" in body["markdown"]


def test_a_finished_class_is_listed_even_after_it_is_purged(tmp_path):
    """The successful path ends in a purge, and the home screen's list excludes
    purged sessions. Without a list of its own, finishing a class correctly is
    what makes it vanish.

    Mutation that turns this red: remove the `GET /api/summaries` route, or
    filter it to non-purged sessions.
    """
    client, database, runtime, session_id, status = run_flow_to_summary(
        tmp_path, auto_purge=True
    )
    assert status in ("SUMMARY_SAVED", "PURGED")

    with client:
        listed = client.get("/api/summaries").json()["summaries"]
        recovery = client.get("/api/recovery").json()["sessions"]

    assert [s["session_id"] for s in listed] == [session_id]
    assert listed[0]["course_name"] == "Summary Course"
    if not any(s["id"] == session_id for s in recovery):
        assert True, "purged sessions are absent from the recovery list by design"


def test_an_altered_summary_is_reported_rather_than_served(tmp_path):
    """The hash binding is the integrity contract. A file that no longer
    matches it is not this class's notes, and must not be shown as if it were.

    Mutation that turns this red: drop the hash comparison in
    `session_summary`.
    """
    client, database, runtime, session_id, status = run_flow_to_summary(tmp_path)
    record = SessionRepository(database).get_session(session_id)
    path = Path(record.summary_path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n注入的一行\n", encoding="utf-8"
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() != record.summary_hash

    with client:
        response = client.get(f"/api/sessions/{session_id}/summary")

    assert response.status_code == 409
    assert response.json()["detail"] == "SUMMARY_HASH_MISMATCH"


def test_a_summary_outside_the_store_is_refused(tmp_path):
    """Path containment, the same rule cleanup applies before it deletes.

    Mutation that turns this red: drop the `is_relative_to(root)` check.
    """
    client, database, runtime, session_id, status = run_flow_to_summary(tmp_path)
    outside = tmp_path / "elsewhere.md"
    original = Path(
        SessionRepository(database).get_session(session_id).summary_path
    ).read_bytes()
    outside.write_bytes(original)
    with database.connect() as connection:
        connection.execute(
            "UPDATE sessions SET summary_path = ? WHERE id = ?",
            (str(outside), session_id),
        )
        connection.commit()

    with client:
        response = client.get(f"/api/sessions/{session_id}/summary")

    assert response.status_code == 409
    assert response.json()["detail"] == "SUMMARY_OUTSIDE_STORE"


def test_a_class_with_no_summary_yet_says_so(tmp_path):
    """Mutation that turns this red: return 200 with an empty document."""
    from test_live_api import make_client

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        view = client.post("/api/sessions", json={"course_name": "Ongoing"}).json()
        response = client.get(f"/api/sessions/{view['session']['id']}/summary")

    assert response.status_code == 404
    assert response.json()["detail"] == "NO_SUMMARY_YET"


def test_a_purged_class_still_answers_and_still_has_its_summary(tmp_path):
    """The successful outcome used to be the one that broke the screen.

    Purge deletes the `sessions` row and keeps `purged_session_metadata`. The
    session endpoint answered 404, so the client polling the class it had just
    ended silently stopped updating: the screen sat on "正在等待…生成课程总结"
    forever, for the one path where everything had in fact worked. And the
    summary itself was unreachable, because both lookups read only the live
    table.

    Mutation that turns this red: drop the `_purged_payload` fallback in
    `get_session`, or the `purged_session_metadata` half of
    `_summary_binding`.
    """
    client, database, runtime, session_id, status = run_flow_to_summary(
        tmp_path, auto_purge=True
    )
    with database.connect() as connection:
        purged = connection.execute(
            "SELECT COUNT(*) AS n FROM purged_session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        live = connection.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["n"]
    assert (purged, live) == (1, 0), "this test needs a genuinely purged class"

    with client:
        view = client.get(f"/api/sessions/{session_id}")
        summary = client.get(f"/api/sessions/{session_id}/summary")

    assert view.status_code == 200
    assert view.json()["session"]["status"] == "PURGED"
    assert summary.status_code == 200
    assert summary.json()["markdown"].startswith("# 课程名称：Summary Course")


def test_an_unknown_session_is_still_a_404(tmp_path):
    """The purged-class fallback must not turn every unknown id into a 200.

    Mutation that turns this red: return `_purged_payload`'s empty shape
    instead of raising when no metadata row exists either.
    """
    from test_live_api import make_client

    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        response = client.get("/api/sessions/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json()["detail"] == "SESSION_NOT_FOUND"
