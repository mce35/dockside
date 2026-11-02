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

class TestDocksidePickerTests:
	def test_empty_image_and_volume_picker_routes(self, monkeypatch) -> None:
		api = Mock(daemon_version="25", list_containers=Mock(return_value=[]), list_images=Mock(return_value=[]), list_volumes=Mock(return_value=[]))
		target = {"name": "ctx", "endpoint": "unix:///docker", "api": api}
		mock_picker_terminal(monkeypatch, ["down", "home", "end", "all", "clear", "toggle", "delete", "unknown", "container_view"])
		with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
			assert dockside._select_images([target], ImagePickerState(), ActionLog(), executor) == "containers"
		mock_picker_terminal(monkeypatch, ["down", "home", "end", "all", "clear", "toggle", "delete", "unknown", "container_view"])
		with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
			assert dockside._select_volumes([target], VolumePickerState(), ActionLog(), executor) == "containers"
		for key, expected in [("container_view", "containers"), ("images", "images"), ("quit", "quit")]:
			mock_picker_terminal(monkeypatch, [key])
			with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
				assert dockside._select_volumes([target], VolumePickerState(), ActionLog(), executor) == expected
		for key, expected in [("quit", "quit"), ("volumes", "volumes")]:
			mock_picker_terminal(monkeypatch, [key])
			with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
				assert dockside._select_images([target], ImagePickerState(), ActionLog(), executor) == expected


	def test_image_delete_pending_and_completed_refresh_branches(self, monkeypatch) -> None:
		class FutureStub:
			def __init__(self, complete): self.complete = complete
			def done(self): return self.complete
		api = Mock(daemon_version="25")
		api.list_containers.return_value = []
		image = {"Id": "sha", "RepoTags": ["repo:tag"]}
		api.list_images.side_effect = [[image], [image], []]
		target = {"name": "ctx", "endpoint": "unix:///docker", "api": api}
		mock_picker_terminal(monkeypatch, ["delete", "delete", "volumes"])
		monkeypatch.setattr(dockside, "_confirm_dialog", lambda *args, **kwargs: True)
		executor = Mock()
		executor.submit.return_value = FutureStub(False)
		log = ActionLog()
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._select_images([target], ImagePickerState(), log, executor) == "volumes"
		assert "already being deleted" in log.entries[-1]
		api.list_images.side_effect = [[image], []]
		mock_picker_terminal(monkeypatch, ["delete", "volumes"])
		executor.submit.return_value = FutureStub(True)
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._select_images([target], ImagePickerState(), ActionLog(), executor) == "volumes"


	def test_volume_sort_render_and_nonreadable_input_branches(self, monkeypatch) -> None:
		api = Mock(daemon_version="25")
		api.list_containers.return_value = []
		api.list_volumes.return_value = [{"Name": "unused", "Driver": "local", "CreatedAt": "2025-01-01T00:00:00Z"}, {"Name": "other", "Driver": "local"}]
		target = {"name": "ctx", "endpoint": "unix:///docker", "api": api}
		for column in ["created", "used"]:
			state = VolumePickerState(sort_column=column)
			mock_picker_terminal(monkeypatch, ["toggle", "down", "images"])
			monkeypatch.setattr(dockside.shutil, "get_terminal_size", lambda fallback: os.terminal_size((45, 4)))
			with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
				assert dockside._select_volumes([target], state, ActionLog(), executor) == "images"
			assert state.selected_keys
		mock_picker_terminal(monkeypatch, ["images"])
		monkeypatch.setattr(dockside.select, "select", Mock(side_effect=[([], [], []), ([77], [], [])]))
		with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
			assert dockside._select_volumes([target], VolumePickerState(), ActionLog(), executor) == "images"


	def test_image_and_volume_delete_cancel_paths_and_return_shortcuts(self, monkeypatch) -> None:
		api = Mock(daemon_version="25")
		api.list_containers.return_value = [{"Id": "c", "Names": ["/web"], "Mounts": [{"Type": "volume", "Name": "data"}]}]
		api.list_images.return_value = [{"Id": "i", "RepoTags": ["repo:tag"]}]
		api.list_volumes.return_value = [{"Name": "data", "Driver": "local"}]
		target = {"name": "ctx", "endpoint": "unix:///docker", "api": api}
		monkeypatch.setattr(dockside, "_confirm_dialog", lambda *args, **kwargs: False)
		mock_picker_terminal(monkeypatch, ["toggle", "toggle", "delete", "container_view"])
		with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
			assert dockside._select_images([target], ImagePickerState(), ActionLog(), executor) == "containers"
		mock_picker_terminal(monkeypatch, ["toggle", "toggle", "delete", "quit"])
		with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=1) as executor:
			assert dockside._select_volumes([target], VolumePickerState(), ActionLog(), executor) == "quit"


	def test_container_picker_refreshes_after_completed_and_failed_future(self, monkeypatch) -> None:
		container = {"Id": "a", "Names": ["/web"], "State": "running"}
		for future_error in [None, RuntimeError("refresh failed")]:
			checks = Mock()
			checks.snapshot.return_value = ({}, "ready")
			future = Mock(done=Mock(return_value=True))
			if future_error:
				future.result.side_effect = future_error
			else:
				future.result.return_value = [container]
			checks.executor.submit.return_value = future
			mock_picker_terminal(monkeypatch, ["clear", "delete"])
			monkeypatch.setattr(dockside, "_render_container_picker", Mock())
			with contextlib.redirect_stdout(io.StringIO()):
				result = dockside._select_containers([container], checks, PickerState(), ActionLog(), -1)
			assert result is not None and result[1] == "delete"
			future.result.assert_called_once()
			assert checks.update.call_count >= 2


	def test_read_picker_key_escape_sequences_and_special_keys(self, monkeypatch) -> None:
		for chars, expected in [
			(["\x1b", "[", "A"], "up"),
			(["\x1b", "O", "Q"], "images"),
			(["\x1b", "O", "P"], "container_view"),
			(["\x1b", "[", "H"], "home"),
			(["\x1b", "[", "4", "~"], "end"),
			(["\x1b", "[", "1", "~"], "home"),
			(["\x1b", "[", "1", "2", "~"], "images"),
			(["\x1b", "[", "9", "~"], ""),
			(["\x1b", "x"], ""),
			(["\x1b", "[", "x"], ""),
			(["q"], "action_down"),
			(["\x11"], "quit"),
		]:
			monkeypatch.setattr(dockside.sys, "stdin", Mock(read=Mock(side_effect=chars)))
			assert dockside._read_picker_key() == expected


	def test_container_picker_handles_selection_sort_actions_and_shortcuts(self, monkeypatch) -> None:
		containers = [
			{"Id": "b", "Names": ["/z"], "State": "running", "_DocksideContext": "prod"},
			{"Id": "a", "Names": ["/a"], "State": "exited", "_DocksideContext": "prod"},
		]
		checks = Mock()
		checks.snapshot.return_value = ({}, "ready")
		state = PickerState()
		mock_picker_terminal(monkeypatch, [
			"up", "home", "end", "down", "left", "right", "sort_direction",
			"toggle", "all", "clear", "toggle", "action_up", "action_down", "inspect",
		])
		monkeypatch.setattr(dockside, "_render_container_picker", Mock())
		with contextlib.redirect_stdout(io.StringIO()):
			result = dockside._select_containers(
				containers, checks, state, ActionLog(), 0, "prod", "unix:///tmp/docker.sock", "27"
			)
		assert result is not None
		selected, action = result
		assert action == "inspect"
		assert len(selected) == 1
		assert state.active_name == dockside._container_key(selected[0])
		assert checks.update.call_count >= 1
		checks.snapshot.assert_called()


	def test_container_picker_handles_empty_list_and_requires_tty(self, monkeypatch) -> None:
		checks = Mock()
		checks.snapshot.return_value = ({}, "ready")
		mock_picker_terminal(monkeypatch, ["volumes"])
		monkeypatch.setattr(dockside, "_render_container_picker", Mock())
		with contextlib.redirect_stdout(io.StringIO()):
			result = dockside._select_containers([], checks, PickerState(), ActionLog(), 0)
		assert result == ([], "volumes")
		monkeypatch.setattr(dockside.sys, "stdin", Mock(isatty=lambda: False))
		with pytest.raises(DockerError, match="interactive terminal"):
			dockside._select_containers([], checks, PickerState(), ActionLog(), 0)


	def test_container_picker_returns_none_when_quitting_with_no_rows(self, monkeypatch) -> None:
		checks = Mock()
		checks.snapshot.return_value = ({}, "ready")
		mock_picker_terminal(monkeypatch, ["quit"])
		monkeypatch.setattr(dockside, "_render_container_picker", Mock())
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._select_containers([], checks, PickerState(), ActionLog(), 0) is None


	def test_confirmation_dialog_navigation_and_cancel(self, monkeypatch) -> None:
		mock_picker_terminal(monkeypatch, [])
		reads = iter([b"\x1b", b"[C", b"\r"])
		monkeypatch.setattr(dockside.os, "read", lambda fd, count: next(reads))
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._confirm_dialog("Delete?", ["prod/db"], ["Details"], "Volumes") is False
		reads = iter([b"q"])
		monkeypatch.setattr(dockside.os, "read", lambda fd, count: next(reads))
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._confirm_delete(["db"]) is False
		with patch.object(dockside, "_confirm_dialog", return_value=True) as confirm:
			assert dockside._confirm_delete(["one", "two"])
		assert "2 selected containers" in confirm.call_args.args[0]
		mock_picker_terminal(monkeypatch, [])
		reads = iter([b"\x1b", b"[D", b"\r"])
		monkeypatch.setattr(dockside.os, "read", lambda fd, count: next(reads))
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._confirm_dialog("Delete?", ["one"]) is True
		reads = iter([b"\x1b"])
		monkeypatch.setattr(dockside.os, "read", lambda fd, count: next(reads))
		monkeypatch.setattr(dockside.select, "select", lambda *args: ([], [], []))
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._confirm_dialog("Delete?", ["one"]) is None


	def test_picker_routes_and_restores_terminal_with_mocked_docker_api(self, monkeypatch) -> None:
		api = Mock()
		api.list_containers.return_value = []
		api.list_images.return_value = [{"Id": "sha256:a", "RepoTags": ["repo:tag"]}]
		api.list_volumes.return_value = [{"Name": "data", "Driver": "local"}]
		targets = [{"name": "test", "endpoint": "unix:///tmp/docker.sock", "api": api}]
		mock_picker_terminal(monkeypatch, ["volumes"])
		with contextlib.redirect_stdout(io.StringIO()):
			with ThreadPoolExecutor(max_workers=1) as executor:
				result = dockside._select_images(targets, ImagePickerState(), ActionLog(), executor)
		assert result == "volumes"
		api.list_images.assert_called_once()


	def test_image_picker_selection_sort_and_background_delete(self, monkeypatch) -> None:
		api = Mock()
		api.daemon_version = "27.0"
		api.list_containers.return_value = []
		api.list_images.return_value = [{"Id": "sha256:a", "RepoTags": ["repo:tag"], "Size": 10, "Created": 1}]
		targets = [{"name": "test", "endpoint": "unix:///tmp/docker.sock", "api": api}]
		log = ActionLog()
		keys = ["down", "home", "end", "left", "right", "sort_direction", "action_up", "action_down", "toggle", "all", "clear", "toggle", "delete", "volumes"]
		mock_picker_terminal(monkeypatch, keys)
		monkeypatch.setattr(dockside, "_confirm_dialog", lambda *args, **kwargs: True)
		with contextlib.redirect_stdout(io.StringIO()):
			with ThreadPoolExecutor(max_workers=1) as executor:
				result = dockside._select_images(targets, ImagePickerState(), log, executor)
		assert result == "volumes"
		api.remove_image.assert_called_once_with("sha256:a")
		assert any("Deleted image test/repo:tag" in entry for entry in log.entries)


	def test_volume_picker_selection_sort_and_background_delete(self, monkeypatch) -> None:
		api = Mock()
		api.daemon_version = "27.0"
		api.list_containers.return_value = [{
			"Id": "db", "Names": ["/db"],
			"Mounts": [{"Type": "volume", "Name": "data"}],
		}]
		api.list_volumes.return_value = [{"Name": "data", "Driver": "local", "CreatedAt": "2025-01-01T00:00:00Z"}]
		targets = [{"name": "test", "endpoint": "unix:///tmp/docker.sock", "api": api}]
		log = ActionLog()
		mock_picker_terminal(monkeypatch, ["down", "home", "end", "left", "right", "sort_direction", "action_up", "action_down", "toggle", "all", "clear", "toggle", "delete", "images"])
		monkeypatch.setattr(dockside, "_confirm_dialog", lambda *args, **kwargs: True)
		with contextlib.redirect_stdout(io.StringIO()):
			with ThreadPoolExecutor(max_workers=1) as executor:
				result = dockside._select_volumes(targets, VolumePickerState(), log, executor)
		assert result == "images"
		api.remove_volume.assert_called_once_with("data")
		assert any("Deleted volume test/data" in entry for entry in log.entries)


	def test_picker_key_maps_function_keys_and_controls(self) -> None:
		for sequence, expected in [("\x1bOP", "container_view"), ("\x1bOQ", "images"), ("\x1bOR", "volumes"), ("\x1b[13~", "volumes")]:
			with patch("sys.stdin", io.StringIO(sequence)):
				assert dockside._read_picker_key() == expected
		for sequence, expected in [("\x04", "delete"), ("\x01", "all"), ("\x13", "stop"), ("\x12", "recreate")]:
			with patch("sys.stdin", io.StringIO(sequence)):
				assert dockside._read_picker_key() == expected


