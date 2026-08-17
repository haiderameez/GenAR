from __future__ import annotations

import re

from genar.llm import Completion, prompt_digest

_FIGURE_LINE = re.compile(r"^\[(?P<id>[a-z_]+\.[a-z_]+)\]\s+(?P<label>.+?):\s*(?P<value>.+)$")
_DIST_HEADER = re.compile(r"^\[(?P<id>[a-z_]+\.[a-z_]+)\]\s+(?P<label>.+?)\s+\(of .+\):$")
_DIST_ROW = re.compile(r"^\s{4}(?P<label>.+?):\s(?P<value>[\d,]+\s+\S.*)$")


def restate_packet(prompt: str) -> str:
    sentences: list[str] = []
    lines = prompt.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        header = _DIST_HEADER.match(line)
        if header:
            rows: list[str] = []
            index += 1
            while index < len(lines) and (row := _DIST_ROW.match(lines[index])):
                rows.append(f"{row.group('label')} ({row.group('value')})")
                index += 1
            if rows:
                shown = "; ".join(rows[:5])
                sentences.append(f"{header.group('label')}: {shown}.")
            continue

        figure = _FIGURE_LINE.match(line)
        if figure:
            value = figure.group("value").strip()
            if value.startswith("rendered directly"):
                index += 1
                continue
            if value.startswith("not "):
                sentences.append(f"{figure.group('label')} was {value}.")
            else:
                sentences.append(f"{figure.group('label')}: {value}.")
        index += 1

    if not sentences:
        return "No approved figures were supplied for this section."
    return " ".join(sentences)


class TemplateClient:
    name = "template"
    model = "test-template-writer"

    def __init__(self) -> None:
        self.calls_made = 0
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str) -> Completion:
        self.calls_made += 1
        self.prompts.append(prompt)
        return Completion(
            text=restate_packet(prompt),
            model=self.model,
            prompt_sha256=prompt_digest(self.model, system, prompt),
        )
