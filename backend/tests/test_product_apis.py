from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def make_client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'product.db'}",
        device_token="device-test",
        simulator_token="sim-test",
        allowed_origins=(),
        max_audio_frame_bytes=4096,
        vad_rms_threshold=450,
        vad_end_silence_ms=900,
        auth_signing_secret="test-signing-secret-that-is-long-enough",
        bootstrap_admin_username="admin",
        bootstrap_admin_password="test-password-that-is-long",
    )
    client = TestClient(create_app(settings))
    client.__enter__()
    login = client.post("/api/v1/auth/login", json={
        "username": "admin",
        "password": "test-password-that-is-long",
    })
    assert login.status_code == 200
    client.headers.update({"Authorization": f"Bearer {login.json()['access_token']}"})
    return client


def test_calendar_reminder_notes_and_audit_exports(tmp_path) -> None:
    with make_client(tmp_path) as client:
        event = client.post("/api/v1/calendar/events", json={
            "title": "Design review",
            "description": "Review roadmap",
            "kind": "meeting",
            "start_at": "2026-12-01T15:00:00+06:00",
            "end_at": "2026-12-01T16:00:00+06:00",
            "timezone_name": "Asia/Dhaka",
            "recurrence_rule": "FREQ=WEEKLY;COUNT=4",
        })
        assert event.status_code == 201
        event_id = event.json()["id"]
        rows = client.get("/api/v1/calendar/events?search=Design").json()
        assert rows["total"] == 1 and rows["items"][0]["id"] == event_id
        assert client.patch(f"/api/v1/calendar/events/{event_id}", json={
            "title": "Updated review",
            "description": "",
            "kind": "meeting",
            "start_at": "2026-12-01T16:00:00+06:00",
            "end_at": None,
            "timezone_name": "Asia/Dhaka",
        }).status_code == 200
        assert client.delete(f"/api/v1/calendar/events/{event_id}").status_code == 204
        assert client.post("/api/v1/calendar/events", json={
            "title": "Invalid time",
            "kind": "event",
            "start_at": "2026-12-01T15:00:00",
            "timezone_name": "Asia/Dhaka",
        }).status_code == 422

        reminder = client.post("/api/v1/reminders", json={
            "title": "Call",
            "due_at": "2026-12-02T09:00:00+06:00",
            "timezone_name": "Asia/Dhaka",
        })
        assert reminder.status_code == 201
        reminder_id = reminder.json()["id"]
        assert client.patch(f"/api/v1/reminders/{reminder_id}", json={"completed": True}).json()["completed"]
        assert client.post("/api/v1/notes", json={"body": "Bring printed notes"}).status_code == 201
        assert client.get("/api/v1/notes").json()["items"][0]["body"] == "Bring printed notes"
        assert client.get("/api/v1/audit").json()["total"] >= 5
        assert "event_type" in client.get("/api/v1/audit/export.csv").text
        assert client.get("/api/v1/audit/export.json").json()


def test_authoritative_games_and_version_conflict(tmp_path) -> None:
    with make_client(tmp_path) as client:
        started = client.post("/api/v1/games/number_guessing/sessions", json={})
        assert started.status_code == 201
        session_id = started.json()["session_id"]
        assert "secret" not in started.json()["state"]
        move = client.post(f"/api/v1/games/sessions/{session_id}/moves", json={
            "expected_version": 1,
            "move": {"guess": 50},
        })
        assert move.status_code == 200 and move.json()["version"] == 2
        stale = client.post(f"/api/v1/games/sessions/{session_id}/moves", json={
            "expected_version": 1,
            "move": {"guess": 40},
        })
        assert stale.status_code == 409
        trivia = client.post("/api/v1/games/trivia/sessions", json={"category": "programming"})
        assert trivia.status_code == 201
        assert "answer" not in trivia.json()["state"]["questions"][0]


def test_todos_and_live_display_page_are_persisted_and_audited(tmp_path) -> None:
    with make_client(tmp_path) as client:
        todo = client.post("/api/v1/todos", json={
            "title": "Review firmware",
            "due_at": "2026-12-03T10:00:00+06:00",
        })
        assert todo.status_code == 201
        todo_id = todo.json()["id"]
        assert todo.json()["due_at"] == "2026-12-03T04:00:00+00:00"
        assert client.get("/api/v1/todos?completed=false").json()["items"][0]["title"] == "Review firmware"
        assert client.patch(f"/api/v1/todos/{todo_id}", json={"completed": True}).json()["completed"]
        assert client.get("/api/v1/todos?completed=false").json()["items"] == []
        assert client.get("/api/v1/todos?completed=true").json()["items"][0]["id"] == todo_id
        assert client.post("/api/v1/todos", json={
            "title": "Naive timestamp",
            "due_at": "2026-12-03T10:00:00",
        }).status_code == 422

        registered = client.post("/api/v1/devices/register", json={
            "device_id": "display-1",
            "name": "ST7735 assistant",
        })
        assert registered.status_code == 201
        page = client.put("/api/v1/devices/display-1/display/page", json={"page": "todos"})
        assert page.status_code == 200
        assert page.json()["delivery"] == "stored_until_reconnect"
        assert client.get("/api/v1/devices/display-1/display").json()["configuration"]["page"] == "todos"
        assert client.put("/api/v1/devices/display-1/display/page", json={"page": "invalid"}).status_code == 422
        assert client.post("/api/v1/todos", json={"title": "Check the live display"}).status_code == 201
        assert client.delete(f"/api/v1/todos/{todo_id}").status_code == 204
        assert client.get("/api/v1/audit").json()["total"] >= 4

        with client.websocket_connect(
            "/api/v1/ws/devices/display-1/audio",
            headers={"Authorization": f"Bearer {registered.json()['device_token']}"},
        ) as websocket:
            websocket.send_json({
                "protocol_version": 1,
                "message_id": "display-hello",
                "device_id": "display-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "device.hello",
                "payload": {},
            })
            assert websocket.receive_json()["type"] == "device.status"
            initial_page = websocket.receive_json()
            assert initial_page["type"] == "display.state"
            assert initial_page["payload"]["items"] == ["Check the live display"]
            changed = client.put("/api/v1/devices/display-1/display/page", json={"page": "notes"})
            assert changed.json()["delivery"] == "sent"
            assert websocket.receive_json()["payload"]["page"] == "notes"


def test_dashboard_apis_require_authentication(tmp_path) -> None:
    with make_client(tmp_path) as client:
        client.headers.pop("Authorization")
        assert client.get("/api/v1/calendar/events").status_code == 401
        assert client.get("/api/v1/devices").status_code == 401
