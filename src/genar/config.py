from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import DatasetNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
REPORTS_DIR = PROJECT_ROOT / "reports"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
OUTPUT_DIR = PROJECT_ROOT / "output"
REVIEW_DIR = PROJECT_ROOT / "review"
STATE_DIR = PROJECT_ROOT / ".genar"


DATASET_ENV_VAR = "GENAR_DATASET"
DATASET_SEARCH_PATHS: tuple[Path, ...] = (
    PROJECT_ROOT / "data" / "Bisoprolol_icsr_sample_1068rows.xlsx",
    PROJECT_ROOT / "data" / "Bisoprolol_icsr_sample_1068rows.csv",
    Path.home() / "Downloads" / "Bisoprolol_icsr_sample_1068rows.xlsx",
    Path.home() / "Downloads" / "Bisoprolol_icsr_sample_1068rows.csv",
)


def load_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}

    applied: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied

def resolve_dataset(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    from_env = os.environ.get(DATASET_ENV_VAR)
    if from_env:
        return Path(from_env)
    for candidate in DATASET_SEARCH_PATHS:
        if candidate.exists():
            return candidate
    raise DatasetNotFoundError(
        "dataset not found. Pass --data /path/to/Bisoprolol_icsr_sample_1068rows.xlsx "
        f"or set {DATASET_ENV_VAR}."
    )

@dataclass(frozen=True)
class LLMSettings:


    model: str = field(default_factory=lambda: os.environ.get("GENAR_MODEL", "gemini-3.6-flash"))
    temperature: float = 0.0

    max_output_tokens: int = 8192

    thinking_level: str = "MINIMAL"
    requests_per_minute: int = field(
        default_factory=lambda: int(os.environ.get("GENAR_RPM", "10"))
    )
    daily_request_budget: int = field(
        default_factory=lambda: int(os.environ.get("GENAR_DAILY_BUDGET", "20"))
    )

    max_retries: int = 6
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 45.0
    api_key_env_var: str = "GEMINI_API_KEY"

    @property
    def min_seconds_between_calls(self) -> float:
        if self.requests_per_minute <= 0:
            return 0.0
        return 60.0 / self.requests_per_minute
