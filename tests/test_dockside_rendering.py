import contextlib
import io
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import dockside
import image_registry
import container_ops
from container_models import ActionLog, ImagePickerState, PickerState, VolumePickerState
from docker_api import DockerAPI, DockerError
from tests.helpers import mock_picker_terminal

class TestDocksideRenderingTests:
	def test_sort_clip_and_render_layout_branches(self, monkeypatch) -> None:
		container = {"Id": "id", "Names": ["/web"], "Status": "running", "Image": "repo:tag",
			"Created": "2025-01-01T00:00:00Z", "Ports": [{"PrivatePort": 80}], "_DocksideContext": "prod"}
		for column in ["id", "status", "context", "created", "ports", "freshness", "image", "name"]:
			items = [dict(container), {**container, "Id": "other", "Names": ["/aaa"]}]
			dockside._sort_containers(items, column, True, {"id": "OUTDATED"})
		assert dockside._paint_span("abc", 4, 2, "red") == "abc"
		assert dockside._truncate_cell("long-value", 3) == "lon"
		assert dockside._truncate_cell("long-value", 5, align_right=True) == "lo..."
		assert dockside._clip_ansi_line("\033[31mabcdef\033[0m", 3).endswith("\033[0m")
		assert dockside._clip_ansi_line("abc", 3) == "abc"
		assert dockside._clip_ansi_line("\033[31abc", 2).endswith("\033[0m")
		images = [{"Id": "b", "RepoTags": ["b:tag"], "Size": 2, "Created": 1, "_DocksideUsed": ["z"]},
			{"Id": "a", "RepoTags": ["a:tag"], "Size": 1, "Created": 2, "_DocksideContext": "prod"}]
		for column in ["context", "name", "sha", "size", "created", "used"]:
			dockside._sort_images(images, ImagePickerState(sort_column=column, sort_ascending=False))
		state = ImagePickerState()
		log = ActionLog()
		log.append("action completed")
		for width, rows in [(12, 1), (50, 5), (120, 9)]:
			monkeypatch.setattr(dockside.shutil, "get_terminal_size", lambda fallback, w=width, h=rows: os.terminal_size((w, h)))
			with contextlib.redirect_stdout(io.StringIO()):
				dockside._render_image_picker(images, 0, {"b"}, state, log, "prod", "sock", "25")
		with contextlib.redirect_stdout(io.StringIO()):
			dockside._render_image_picker(images, 0, {"a"}, state, log, "prod", "sock", "25")
		for rows in [1, 2, 5, 7, 14]:
			monkeypatch.setattr(dockside.shutil, "get_terminal_size", lambda fallback, h=rows: os.terminal_size((35, h)))
			with contextlib.redirect_stdout(io.StringIO()):
				dockside._render_container_picker([container], 0, {"id"}, {"id": "OUTDATED"}, "pending", log, "created", False, "prod", "sock", "25")
		with contextlib.redirect_stdout(io.StringIO()):
			dockside._render_container_picker([container, {**container, "Id": "other"}], 0, {"other"}, {}, "ready", log, context_name="prod")
		with contextlib.redirect_stdout(io.StringIO()):
			dockside._render_container_picker([], 0, set(), {}, "ready", ActionLog())
		assert dockside._sort_label("name", "name", False).endswith("↓")
		assert dockside._image_column_label("size", ImagePickerState(sort_column="size"), 8).endswith("↑")


	def test_container_renderer_orders_columns_and_shows_short_id(self, monkeypatch) -> None:
		monkeypatch.setattr(dockside.shutil, "get_terminal_size", lambda _: os.terminal_size((180, 24)))
		container = {
			"Id": "1234567890abcdef1234567890abcdef", "Names": ["/web"],
			"Status": "Up 1 hour", "State": "running", "Image": "repo:tag",
			"_DocksideContext": "prod", "Ports": [],
		}
		output = io.StringIO()
		with contextlib.redirect_stdout(output):
			dockside._render_container_picker(
				[container], 0, set(), {}, "registry checks", ActionLog(),
				context_name="prod", docker_endpoint="unix:///tmp/docker.sock", daemon_version="27.0",
			)
		text = output.getvalue()
		header = next(line for line in text.splitlines() if "CONTEXT" in line and "STATUS" in line)
		row = next(line for line in text.splitlines() if "1234567890ab" in line)
		assert header.index("CONTEXT") < header.index("CONTAINER ID") < header.index("STATUS") < header.index("NAME") < header.index("IMAGE")
		assert "1234567890ab" in row and "1234567890..." not in row
		assert "prod" in row and "web" in row


	def test_image_renderer_expands_name_and_sha_columns_on_wide_terminal(self, monkeypatch) -> None:
		monkeypatch.setattr(dockside.shutil, "get_terminal_size", lambda _: os.terminal_size((260, 24)))
		image = {"Id": "sha256:" + "a" * 64, "RepoTags": ["registry.example/team/application:release"], "Size": 1024, "Created": 1, "_DocksideContext": "prod", "_DocksideUsed": []}
		output = io.StringIO()
		with contextlib.redirect_stdout(output):
			dockside._render_image_picker([image], 0, set(), ImagePickerState(), ActionLog(), "prod", "unix:///tmp/docker.sock", "27.0")
		lines = output.getvalue().splitlines()
		header = next(line for line in lines if "CONTEXT" in line and "SHA" in line)
		assert len(header) == 259
		assert "(none)" in output.getvalue()


	def test_clip_ansi_line_preserves_reset_after_clip(self) -> None:
		clipped = dockside._clip_ansi_line("\033[31mabcdef\033[0m", 3)
		assert clipped.endswith("\033[0m")
		assert "abc" in clipped


