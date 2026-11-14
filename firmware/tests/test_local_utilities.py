import subprocess
from pathlib import Path


def test_native_game_utility_state_machines(tmp_path: Path) -> None:
    firmware = Path(__file__).resolve().parents[1]
    stubs = firmware / "tests" / "host_stubs"
    executable = tmp_path / "game-utilities"
    subprocess.run(
        [
            "c++", "-std=c++17", "-I", str(stubs), "-I", str(firmware / "include"),
            str(firmware / "main" / "device_games.cpp"), str(stubs / "games_test.cpp"),
            "-o", str(executable),
        ],
        check=True,
    )
    subprocess.run([str(executable)], check=True)
