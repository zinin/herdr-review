"""herdr-review: multi-agent code review orchestrated inside herdr."""
from pathlib import Path

__version__ = "0.2.0"

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
RUNNER_PATH = PACKAGE_ROOT / "bin" / "herdr-review"
