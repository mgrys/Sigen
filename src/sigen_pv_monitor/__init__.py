"""Punkt wejscia dla `uv run sigen-pv-monitor` - uruchamia aplikacje tak samo jak `python main.py`."""
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    sys.path.insert(0, str(ROOT))
    runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
