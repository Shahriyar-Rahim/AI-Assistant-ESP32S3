import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NETWORK = (ROOT / "main" / "device_network.cpp").read_text()
SCHEDULE = (ROOT / "main" / "device_schedule.cpp").read_text()


def test_setup_commands_are_handled_by_firmware_and_listed_in_protocol() -> None:
    schema = json.loads((ROOT.parent / "shared" / "protocol" / "control.schema.json").read_text())
    text = json.dumps(schema)
    for message in ("device.setup", "schedule.sync", "schedule.fired"):
        assert message in text
        assert message in NETWORK
    assert 'strcmp(action, "open_portal")' in NETWORK
    assert "nvs_erase_all(handle)" in NETWORK
    assert '"portal_once"' in NETWORK
    assert "PORTAL_SESSION_TIMEOUT_MS" in NETWORK


def test_local_scheduler_runs_without_the_backend() -> None:
    assert "device_schedule.cpp" in (ROOT / "main" / "CMakeLists.txt").read_text()
    assert "nvs_set_blob" in SCHEDULE and "time(nullptr)" in SCHEDULE
    assert '"local_schedule"' in NETWORK
    assert "DeviceSchedule::start(report_schedule_fired)" in NETWORK
    assert "if (secure_backend) {\n        const esp_sntp_config_t" not in NETWORK


def test_hardware_pin_map_is_untouched_by_setup_work() -> None:
    assert "device_schedule" not in (ROOT / "include" / "hardware_config.h").read_text()
