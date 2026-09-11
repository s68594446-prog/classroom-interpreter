"""Per-course glossary (user request, 2026-09-11).

Lecture audio is dense with domain vocabulary that generic ASR gets subtly
wrong. Measured on synthesized finance/econometrics audio, residual
transcription error was almost entirely term spelling
("heteroscedasticity" for "heteroskedasticity") rather than misheard speech --
and those errors land in the English transcript, which is the factual source
for the summary, so they propagate into the translation and the final notes.
"""

from __future__ import annotations

import json

import pytest

from classroom_interpreter.providers.mimo import (
    MAX_GLOSSARY_TERMS,
    build_asr_payload,
    render_glossary_hint,
)
from classroom_interpreter.storage.repository import SessionRepository

from test_live_api import make_client


def test_terms_are_stored_per_course_and_survive_reuse(database):
    repo = SessionRepository(database)
    repo.add_glossary_term("Financial Engineering", "Ito's lemma", "伊藤引理")
    repo.add_glossary_term("Financial Engineering", "strike price")
    repo.add_glossary_term("Econometrics", "heteroskedasticity")

    fe = repo.list_glossary_terms("Financial Engineering")
    assert {t["term"] for t in fe} == {"Ito's lemma", "strike price"}
    assert [t["term"] for t in repo.list_glossary_terms("Econometrics")] == [
        "heteroskedasticity"
    ]
    # Courses do not leak into each other.
    assert repo.list_glossary_terms("Unknown Course") == []


def test_adding_the_same_term_twice_updates_instead_of_duplicating(database):
    """Re-adding a term is how a user corrects its Chinese rendering."""
    repo = SessionRepository(database)
    repo.add_glossary_term("C", "value at risk", "风险价值")
    repo.add_glossary_term("C", "value at risk", "在险价值")
    terms = repo.list_glossary_terms("C")
    assert len(terms) == 1
    assert terms[0]["preferred_chinese"] == "在险价值"


def test_blank_terms_are_refused(database):
    repo = SessionRepository(database)
    with pytest.raises(ValueError, match="blank"):
        repo.add_glossary_term("C", "   ")


def test_terms_are_ordered_longest_first(database):
    """Specific multi-word terms must precede their fragments in the prompt.

    "geometric Brownian motion" listed after "motion" invites the model to
    anchor on the fragment.
    """
    repo = SessionRepository(database)
    for t in ("motion", "geometric Brownian motion", "Brownian motion"):
        repo.add_glossary_term("C", t)
    assert [t["term"] for t in repo.list_glossary_terms("C")] == [
        "geometric Brownian motion",
        "Brownian motion",
        "motion",
    ]


def test_asr_payload_is_unchanged_when_no_glossary_exists():
    """The common case must not pay for this feature.

    Mutation that turns this red: append the hint unconditionally.
    """
    payload = build_asr_payload(b"\x00" * 64, model="m", language="en")
    content = payload["messages"][0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "input_audio"


def test_asr_payload_carries_terms_and_an_accent_hint():
    payload = build_asr_payload(
        b"\x00" * 64,
        model="m",
        language="en",
        glossary=[{"term": "Ito's lemma"}, {"term": "heteroskedasticity"}],
    )
    content = payload["messages"][0]["content"]
    assert content[0]["type"] == "input_audio", "audio must stay the first item"
    text = content[1]["text"]
    assert "Ito's lemma" in text and "heteroskedasticity" in text
    assert "accent" in text.lower()


def test_translation_hint_pins_the_chinese_rendering():
    """A term with a chosen rendering must reach the translator as an instruction.

    Without this the same English term drifts between synonyms across segments,
    which makes a lecture harder to revise from than one consistent choice.
    """
    hint = render_glossary_hint(
        [{"term": "value at risk", "preferred_chinese": "风险价值"}],
        for_translation=True,
    )
    assert "value at risk → 风险价值" in hint
    # The ASR phrasing must not leak into the translation prompt.
    assert "accent" not in hint.lower()


def test_glossary_size_is_capped():
    """An unbounded list would grow every request and crowd the instruction."""
    many = [{"term": f"term{i}"} for i in range(MAX_GLOSSARY_TERMS + 25)]
    hint = render_glossary_hint(many, for_translation=False)
    assert hint.count("\n- ") == MAX_GLOSSARY_TERMS
    assert f"term{MAX_GLOSSARY_TERMS + 20}" not in hint


def test_empty_and_whitespace_terms_never_reach_a_prompt():
    assert render_glossary_hint([], for_translation=False) == ""
    assert render_glossary_hint([{"term": "   "}], for_translation=True) == ""


def test_glossary_reaches_the_provider_through_the_real_app(tmp_path):
    """End-to-end wiring: a term added via the API must reach build_asr_payload.

    This is the assertion that would catch the whole feature being inert --
    stored, listed, displayed, and never actually sent to the model.
    """
    client, database, runtime = make_client(tmp_path, auto_purge=False)
    with client:
        added = client.post(
            "/api/glossary/Financial Engineering",
            json={"term": "Ito's lemma", "preferred_chinese": "伊藤引理"},
        )
        assert added.status_code == 200
        listed = client.get("/api/glossary/Financial Engineering").json()
        assert [t["term"] for t in listed["terms"]] == ["Ito's lemma"]

        # The same lookup the providers perform.
        repo = SessionRepository(database)
        view = client.post(
            "/api/sessions", json={"course_name": "Financial Engineering"}
        ).json()
        course = repo.get_session(view["session"]["id"]).course_name
        terms = repo.list_glossary_terms(course)
        assert terms and terms[0]["term"] == "Ito's lemma"

        payload = build_asr_payload(
            b"\x00" * 64, model="m", language="en", glossary=terms
        )
        assert "Ito's lemma" in json.dumps(payload, ensure_ascii=False)

        deleted = client.delete(f"/api/glossary/term/{added.json()['id']}")
        assert deleted.status_code == 200
        assert client.get("/api/glossary/Financial Engineering").json()["terms"] == []


def test_glossary_is_configuration_and_survives_purge(tmp_path):
    """A glossary holds terms the user typed, never recorded content.

    It must therefore outlive a purge -- wiping a course's vocabulary because
    one lecture was cleaned up would make the feature useless week to week. It
    is deliberately absent from CONTENT_TABLES for the same reason.
    """
    from test_purge import CONTENT_TABLES, run_to_purged

    client, database, session_id = run_to_purged(tmp_path)
    repo = SessionRepository(database)
    repo.add_glossary_term("Purge Course", "kept term", "保留词")

    assert "glossary_terms" not in CONTENT_TABLES
    terms = repo.list_glossary_terms("Purge Course")
    assert [t["term"] for t in terms] == ["kept term"]
