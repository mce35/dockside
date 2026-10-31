from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class PickerState:
	active_name: str | None = None
	active_index: int = 0
	selected_names: set[str] = field(default_factory=set)
	sort_column: str = "name"
	sort_ascending: bool = True


@dataclass
class RecreatePlan:
	container_id: str
	name: str
	image: str
	inspection: dict[str, Any]
	payload: dict[str, Any]
	was_running: bool
	auto_remove: bool
	pull_first: bool


class ActionLog:
	def __init__(self, max_entries: int = 1000) -> None:
		self.entries: list[str] = []
		self.max_entries = max_entries
		self.scroll_offset = 0
		self.refresh_generation = 0
		self.lock = threading.Lock()

	def append(self, message: str, *, refresh: bool = False) -> None:
		timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
		with self.lock:
			self.entries.append(f"[{timestamp}] {message}")
			if refresh:
				self.refresh_generation += 1
			if len(self.entries) > self.max_entries:
				del self.entries[: len(self.entries) - self.max_entries]

	def completion_generation(self) -> int:
		with self.lock:
			return self.refresh_generation

	def scroll(self, direction: int) -> None:
		with self.lock:
			self.scroll_offset = max(
				0, min(max(0, len(self.entries) - 1), self.scroll_offset + direction)
			)

	def visible(self, line_count: int) -> tuple[list[str], int, int]:
		with self.lock:
			total = len(self.entries)
			end = max(0, len(self.entries) - self.scroll_offset)
			start = max(0, end - line_count)
			return self.entries[start:end], start, total
