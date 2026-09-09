from pathlib import Path

from fastapi.testclient import TestClient

from classroom_interpreter.config import Settings
from classroom_interpreter.domain.models import HealthSnapshot, WebSocketEvent
from classroom_interpreter.main import create_app
from classroom_interpreter.storage import Database


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        classroom_interpreter_data_dir=tmp_path,
    )
    app = create_app(settings=settings, database=Database(settings.database_path))
    return TestClient(app)


def test_backend_starts_and_health_is_explicitly_actionable(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        response = client.get("/health")
        assert response.status_code == 200
        health = HealthSnapshot.model_validate(response.json())
        assert health.phase == "PHASE_1_SKELETON"  # config default until packaging
        assert {component.name for component in health.components} == {
            "microphone",
            "storage",
            "asr",
            "translation",
            "provider",
            "runtime-supervision",
        }
        by_name = {c.name: c for c in health.components}
        # No runtime wired and no key configured: honest unknowns, actionable red.
        assert by_name["microphone"].status == "unknown"
        assert by_name["microphone"].reason_code == "CAPTURE_NOT_ATTACHED"
        assert by_name["provider"].status == "red"
        assert by_name["provider"].reason_code == "MIMO_KEY_MISSING"


def test_root_serves_frontend_and_app_info_reports_runtime(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        info = client.get("/api/app-info").json()
        assert info["status"] == "runtime"
        assert info["name"] == "Classroom Interpreter"
        root = client.get("/")
        # Static frontend is served when built (Single app surface).
        assert root.status_code == 200
        assert "text/html" in root.headers["content-type"]


def test_capture_health_is_opt_in_and_content_free(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        assert client.get('/health/capture').json() == {
            'capture_active': False, 'reason_code': 'CAPTURE_NOT_ATTACHED'}
        class FakeCapture:
            def snapshot(self):
                return {'capture_active': True, 'queue_depth': 0}
        client.app.state.audio_listener = FakeCapture()
        assert client.get('/health/capture').json() == {'capture_active': True, 'queue_depth': 0}


def test_websocket_event_contract(tmp_path: Path) -> None:
    with build_client(tmp_path) as client:
        with client.websocket_connect("/ws/sessions/session-1") as websocket:
            ready = WebSocketEvent.model_validate(websocket.receive_json())
            assert ready.kind == "system.ready"
            assert ready.event_sequence == 0
            assert ready.session_id == "session-1"
            websocket.send_text("ping")
            pong = WebSocketEvent.model_validate(websocket.receive_json())
            assert pong.kind == "system.pong"
            # Pongs are ephemeral liveness markers and never consume a
            # persisted event sequence.
            assert pong.event_sequence == 0
