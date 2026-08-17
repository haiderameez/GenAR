from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import STATE_DIR, LLMSettings
from .errors import (
    IncompleteResponseError,
    LLMConfigurationError,
    LLMError,
    QuotaExceededError,
)


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    prompt_sha256: str

class LLMClient(Protocol):
    name: str
    model: str

    def complete(self, system: str, prompt: str) -> Completion: ...

def prompt_digest(model: str, system: str, prompt: str) -> str:
    payload = json.dumps([model, system, prompt], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

class DailyBudget:

    def __init__(self, limit: int, model: str = "default", directory: Path = STATE_DIR) -> None:
        self.limit = limit
        self.model = model
        self.path = directory / "daily_budget.json"

    def _load(self) -> tuple[str, dict[str, int]]:
        today = time.strftime("%Y-%m-%d")
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("date") == today:
                    counts = data.get("models", {})
                    return today, {str(k): int(v) for k, v in counts.items()}
            except (json.JSONDecodeError, OSError, ValueError, AttributeError):
                pass
        return today, {}

    @property
    def used(self) -> int:
        return self._load()[1].get(self.model, 0)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def claim(self, count: int = 1) -> None:
        today, counts = self._load()
        used = counts.get(self.model, 0)
        if used + count > self.limit:
            raise QuotaExceededError(
                f"daily request budget for {self.model} exhausted "
                f"({used}/{self.limit} calls used today). Provider quotas are per "
                "model, so --model <another-model> gives a fresh allowance. "
                "GENAR_DAILY_BUDGET raises this counter."
            )
        counts[self.model] = used + count
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"date": today, "models": counts}, indent=2), encoding="utf-8"
        )

class RateLimiter:

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = max(0.0, min_interval_seconds)
        self._last_call: float | None = None

    def wait(self, sleep=time.sleep, now=time.monotonic) -> float:
        if self.min_interval <= 0:
            return 0.0
        if self._last_call is not None:
            elapsed = now() - self._last_call
            pause = self.min_interval - elapsed
            if pause > 0:
                sleep(pause)
                self._last_call = now()
                return pause
        self._last_call = now()
        return 0.0

class GeminiClient:

    name = "gemini"

    def __init__(self, settings: LLMSettings) -> None:
        api_key = os.environ.get(settings.api_key_env_var)
        if not api_key:
            raise LLMConfigurationError(
                f"{settings.api_key_env_var} is not set. Export your API key or put "
                "it in .env before generating a report."
            )
        try:
            from google import genai
        except ImportError as exc:
            raise LLMConfigurationError("google-genai is not installed; pip install -r requirements.txt") from exc

        logging.getLogger("google_genai").setLevel(logging.ERROR)

        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self.settings = settings
        self.model = settings.model
        self.budget = DailyBudget(settings.daily_request_budget, settings.model)
        self.limiter = RateLimiter(settings.min_seconds_between_calls)
        self.calls_made = 0

    def complete(self, system: str, prompt: str) -> Completion:
        digest = prompt_digest(self.model, system, prompt)
        self.budget.claim()
        text = self._call_with_retries(system, prompt)
        self.calls_made += 1
        return Completion(text=text, model=self.model, prompt_sha256=digest)

    def _call_with_retries(self, system: str, prompt: str) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.settings.temperature,
            max_output_tokens=self.settings.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=self.settings.thinking_level),
        )
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries):
            self.limiter.wait()
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=prompt, config=config
                )
                _reject_incomplete(response, self.settings)
                text = (response.text or "").strip()
                if not text:
                    raise LLMError("model returned an empty response")
                return text
            except Exception as exc:
                last_error = exc
                if _is_daily_quota_exhausted(exc):
                    raise QuotaExceededError(_daily_quota_message(exc, self.settings)) from exc
                if not _is_retryable(exc):
                    raise
                if attempt == self.settings.max_retries - 1:
                    raise LLMError(_exhausted_message(exc, self.settings)) from exc
                delay = _retry_after(exc) or min(
                    self.settings.retry_base_seconds * (2**attempt) + random.uniform(0, 1.0),
                    self.settings.retry_max_seconds,
                )
                time.sleep(delay)
        raise LLMError(f"model call failed after retries: {last_error}")

INCOMPLETE_FINISH_REASONS = {
    "MAX_TOKENS": (
        "the response hit the output token limit and was cut off. Raise "
        "max_output_tokens, or lower thinking_level -- on Gemini 3.x, reasoning "
        "tokens come out of the same budget as the answer"
    ),
    "SAFETY": "the response was stopped by a safety filter",
    "RECITATION": "the response was stopped as recitation",
    "PROHIBITED_CONTENT": "the response was stopped as prohibited content",
    "BLOCKLIST": "the response was stopped by a blocklist",
    "SPII": "the response was stopped as containing sensitive personal information",
    "MALFORMED_FUNCTION_CALL": "the response was a malformed function call",
}


def _reject_incomplete(response: object, settings: "LLMSettings") -> None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return
    reason = getattr(candidates[0], "finish_reason", None)
    name = getattr(reason, "name", None) or getattr(reason, "value", None) or str(reason or "")
    explanation = INCOMPLETE_FINISH_REASONS.get(name.upper())
    if explanation:
        raise IncompleteResponseError(
            f"model stopped early (finish_reason={name}): {explanation}. "
            f"Current limit is {settings.max_output_tokens} tokens at "
            f"thinking_level={settings.thinking_level}."
        )

def _exhausted_message(exc: Exception, settings: "LLMSettings") -> str:
    return (
        f"model call failed after {settings.max_retries} attempts against "
        f"{settings.model}: {exc}\n"
        "Options: re-run the command, or pick another model with --model."
    )

_DAILY_QUOTA_MARKERS = ("perday", "per day", "requests per day", "requestsperday")

def _is_daily_quota_exhausted(exc: Exception) -> bool:
    text = str(exc).lower()
    if "429" not in text and "resource_exhausted" not in text:
        return False
    return any(marker in text for marker in _DAILY_QUOTA_MARKERS)

def _daily_quota_message(exc: Exception, settings: "LLMSettings") -> str:
    limit = re.search(r"quotavalue['\"]?:\s*['\"]?(\d+)", str(exc), re.IGNORECASE)
    allowance = f" The provider reports a limit of {limit.group(1)} requests per day." if limit else ""
    return (
        f"the daily free-tier quota for {settings.model} is exhausted.{allowance} "
        "Retrying will not help until it resets. Quotas are per model, so "
        "--model <another-model> gives a fresh allowance."
    )

def _is_retryable(exc: Exception) -> bool:
    if _is_daily_quota_exhausted(exc):
        return False
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("429", "resource_exhausted", "rate limit", "503", "unavailable", "500", "timeout")
    )

def _retry_after(exc: Exception) -> float | None:
    match = re.search(r"retry[- ]?after[\"':\s]+(\d+(?:\.\d+)?)", str(exc), re.IGNORECASE)
    if match:
        return float(match.group(1))
    match = re.search(r"retryDelay[\"':\s]+[\"']?(\d+(?:\.\d+)?)s", str(exc))
    return float(match.group(1)) if match else None
