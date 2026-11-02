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

class TestDocksideContextTests:
	def test_main_routes_views_contexts_actions_and_exit_paths(self, monkeypatch, capsys) -> None:
		class Checks:
			def __init__(self, targets, executor):
				self.targets, self.executor, self.api = targets, executor, self
			def list_containers(self): return []
			def update(self, containers): pass
			def invalidate(self, containers): self.invalidated = containers
		target = {"name": "default", "endpoint": "unix:///docker", "api": Mock(daemon_version="25")}
		monkeypatch.setattr(dockside.sys, "argv", ["dockside"])
		with patch.object(dockside, "_initial_docker_targets", return_value=[target]), \
			patch.object(dockside, "ContextRegistryChecks", Checks), \
			patch.object(dockside, "_select_containers", side_effect=[([], "images"), None]), \
			patch.object(dockside, "_select_images", return_value="containers"):
			assert dockside.main() == 0
		with patch.object(dockside, "_initial_docker_targets", return_value=[target]), \
			patch.object(dockside, "ContextRegistryChecks", Checks), \
			patch.object(dockside, "_select_containers", side_effect=[([], "images"), None]), \
			patch.object(dockside, "_select_images", return_value="volumes"), \
			patch.object(dockside, "_select_volumes", return_value="quit"):
			assert dockside.main() == 0
		with patch.object(dockside, "_initial_docker_targets", return_value=[target]), \
			patch.object(dockside, "ContextRegistryChecks", Checks), \
			patch.object(dockside, "_select_containers", side_effect=[([], "contexts"), None]), \
			patch.object(dockside, "_docker_contexts", return_value=[]), \
			patch.object(dockside, "_pick_docker_context", return_value=None):
			assert dockside.main() == 0
		new_target = {"name": "prod", "endpoint": "ssh://prod", "api": Mock(daemon_version="25")}
		context = {"Name": "prod"}
		with patch.object(dockside, "_initial_docker_targets", return_value=[target]), \
			patch.object(dockside, "ContextRegistryChecks", Checks), \
			patch.object(dockside, "_select_containers", side_effect=[([], "contexts"), None]), \
			patch.object(dockside, "_docker_contexts", return_value=[context]), \
			patch.object(dockside, "_pick_docker_context", return_value=[context]), \
			patch.object(dockside, "_targets_from_contexts", return_value=[new_target]):
			assert dockside.main() == 0
		with patch.object(dockside, "_initial_docker_targets", return_value=[target]), \
			patch.object(dockside, "ContextRegistryChecks", Checks), \
			patch.object(dockside, "_select_containers", side_effect=[([], "contexts"), None]), \
			patch.object(dockside, "_docker_contexts", return_value=[context]), \
			patch.object(dockside, "_pick_docker_context", return_value=[context]), \
			patch.object(dockside, "_targets_from_contexts", side_effect=DockerError("bad context")):
			assert dockside.main() == 0
		with patch.object(dockside, "_initial_docker_targets", return_value=[target]), \
			patch.object(dockside, "ContextRegistryChecks", Checks), \
			patch.object(dockside, "_select_containers", side_effect=[([], "pull"), None]), \
			patch.object(dockside, "_apply_context_action") as apply:
			assert dockside.main() == 0
			apply.assert_called_once()
		with patch.object(dockside, "_initial_docker_targets", side_effect=DockerError("offline")):
			assert dockside.main() == 1
		assert "Error: offline" in capsys.readouterr().err
		with patch.object(dockside, "_initial_docker_targets", side_effect=KeyboardInterrupt):
			assert dockside.main() == 0
		assert "Goodbye" in capsys.readouterr().out


	def test_context_registry_checks_propagate_and_invalidate(self) -> None:
		with ThreadPoolExecutor(max_workers=1) as executor:
			api = Mock()
			api.list_containers.return_value = [{"Id": "id", "Image": "x", "ImageID": "sha"}]
			checks = dockside.ContextRegistryChecks([{"name": "prod", "api": api}], executor)
			listed = checks.list_containers()
			assert listed[0]["_DocksideKey"] == "prod\0id"
			checks.update(listed)
			checks.checks["prod"].futures["x", "sha"].result = Mock(return_value="CURRENT")
			freshness, progress = checks.snapshot(listed)
			assert freshness["prod\0id"] in {"CHECKING", "CURRENT"}
			assert "prod:" in progress
			checks.invalidate(listed)
			checks.update([])
			assert checks.snapshot([]) == ({}, "")


	def test_docker_context_picker_toggles_selects_and_cancels(self, monkeypatch) -> None:
		contexts = [
			{"Name": "default", "DockerEndpoint": "unix:///default", "Current": True},
			{"Name": "prod", "DockerEndpoint": "ssh://prod", "Error": "unavailable"},
		]
		mock_picker_terminal(monkeypatch, ["down", "toggle", "home", "toggle", "confirm"])
		with contextlib.redirect_stdout(io.StringIO()):
			selected = dockside._pick_docker_context(contexts, set())
		assert selected == [contexts[1]]
		mock_picker_terminal(monkeypatch, ["home", "toggle", "confirm"])
		with contextlib.redirect_stdout(io.StringIO()):
			assert dockside._pick_docker_context(contexts, {"default"}) is None
		assert dockside._pick_docker_context([], set()) is None


	def test_docker_context_listing_parses_valid_rows_and_errors(self) -> None:
		valid = json.dumps({"Name": "default", "DockerEndpoint": "unix:///docker", "Current": True})
		with patch.object(dockside.subprocess, "run", return_value=Mock(returncode=0, stdout=valid + "\n{}\n", stderr="")):
			assert dockside._docker_contexts() == [json.loads(valid)]
		with patch.object(dockside.subprocess, "run", side_effect=OSError("docker missing")):
			with pytest.raises(DockerError, match="Could not list Docker contexts"):
				dockside._docker_contexts()
		with patch.object(dockside.subprocess, "run", return_value=Mock(returncode=1, stdout="", stderr="permission denied")):
			with pytest.raises(DockerError, match="permission denied"):
				dockside._docker_contexts()
		with patch.object(dockside.subprocess, "run", return_value=Mock(returncode=0, stdout="not json", stderr="")):
			with pytest.raises(DockerError, match="invalid context list"):
				dockside._docker_contexts()


	def test_context_arguments_and_targets_validation(self, monkeypatch) -> None:
		assert dockside._comma_separated_values(" a, b ", "--contexts") == ["a", "b"]
		with pytest.raises(DockerError):
			dockside._comma_separated_values("a,,b", "--contexts")
		assert dockside._docker_host_endpoint() == "unix:///var/run/docker.sock"
		monkeypatch.setenv("DOCKER_HOST", "ssh://remote")
		assert dockside._docker_host_endpoint() == "ssh://remote"
		with patch.object(dockside, "DockerAPI", side_effect=lambda endpoint: {"endpoint": endpoint}):
			targets = dockside._socket_targets("/one,/two")
		assert [target["name"] for target in targets] == ["socket1", "socket2"]
		with patch.object(dockside, "DockerAPI", side_effect=lambda endpoint: {"endpoint": endpoint}):
			assert dockside._targets_from_contexts([{"Name": "prod", "DockerEndpoint": "ssh://prod"}])[0]["name"] == "prod"


	def test_recreate_context_batch_cancellation_and_background_submission(self) -> None:
		api = Mock()
		container = {"Id": "abc", "Names": ["/web"], "Image": "img", "_DocksideContext": "prod"}
		log = ActionLog()
		executor = Mock()
		plan = Mock(image="example/web:latest", auto_remove=True)
		with patch.object(dockside, "_prepare_recreate", side_effect=DockerError("bad config")):
			dockside._recreate_context_batch([(api, container)], executor, log)
		assert "not all selected" in log.entries[-1]
		with patch.object(dockside, "_prepare_recreate", return_value=plan), patch.object(dockside, "_confirm_dialog", return_value=None):
			dockside._recreate_context_batch([(api, container)], executor, log)
		assert "cancelled for all" in log.entries[-1]
		with patch.object(dockside, "_prepare_recreate", return_value=plan), patch.object(dockside, "_confirm_dialog", side_effect=[True, False]):
			dockside._recreate_context_batch([(api, container)], executor, log)
		assert "cancelled for all" in log.entries[-1]
		with patch.object(dockside, "_prepare_recreate", return_value=plan), patch.object(dockside, "_confirm_dialog", side_effect=[True, True]):
			dockside._recreate_context_batch([(api, container)], executor, log)
		assert plan.pull_first is True
		executor.submit.assert_called_once_with(dockside._run_recreate_logged, api, plan, log)


	def test_initial_docker_targets_selects_context_or_direct_host(self, monkeypatch) -> None:
		context = {"Name": "prod", "DockerEndpoint": "unix:///prod", "Current": True}
		monkeypatch.setattr(dockside.shutil, "which", lambda _: "/usr/bin/docker")
		monkeypatch.setattr(dockside, "_docker_contexts", lambda: [context])
		monkeypatch.setattr(dockside, "DockerAPI", lambda endpoint: Mock(endpoint=endpoint))
		assert dockside._initial_docker_targets(None, None)[0]["name"] == "prod"
		monkeypatch.setenv("DOCKER_HOST", "unix:///direct")
		monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
		assert dockside._initial_docker_targets(None, None)[0]["endpoint"] == "unix:///direct"
		monkeypatch.setattr(dockside.shutil, "which", lambda _: None)
		monkeypatch.setenv("DOCKER_CONTEXT", "prod")
		with pytest.raises(DockerError, match="DOCKER_CONTEXT requires"):
			dockside._initial_docker_targets(None, None)
		monkeypatch.setattr(dockside.shutil, "which", lambda _: "/usr/bin/docker")
		with patch.object(dockside, "_docker_contexts", return_value=[context]), patch.object(dockside, "DockerAPI", side_effect=lambda endpoint: Mock(endpoint=endpoint)):
			assert len(dockside._initial_docker_targets("prod,prod", None)) == 1
			with pytest.raises(DockerError, match="context not found"):
				dockside._initial_docker_targets("missing", None)
		monkeypatch.setattr(dockside.shutil, "which", lambda _: None)
		with pytest.raises(DockerError, match="--contexts requires"):
			dockside._initial_docker_targets("prod", None)


	def test_apply_picker_and_context_actions_use_mocked_api_and_executor(self) -> None:
		api = Mock()
		container = {"Id": "abc", "Names": ["/web"], "State": "running", "_DocksideContext": "prod"}
		executor = Mock()
		log = ActionLog()
		with patch.object(dockside, "_confirm_delete", return_value=False):
			dockside._apply_picker_action(api, [container], "delete", executor, log)
		assert "Deletion cancelled" in log.entries[-1]
		with patch.object(dockside, "_show_container_logs", side_effect=DockerError("pager absent")):
			dockside._apply_picker_action(api, [container], "logs", executor, log)
		assert "ERROR displaying logs" in log.entries[-1]
		with patch.object(dockside, "_show_container_inspection", side_effect=OSError("pager absent")):
			dockside._apply_picker_action(api, [container], "inspect", executor, log)
		assert "ERROR inspecting prod/web" in log.entries[-1]
		with patch.object(dockside, "_recreate_context_batch") as recreate:
			dockside._apply_picker_action(api, [container], "recreate", executor, log)
		recreate.assert_called_once()
		dockside._apply_picker_action(api, [container], "stop", executor, log)
		executor.submit.assert_called_once_with(dockside._run_background_action, api, container, "stop", log)
		target = {"name": "prod", "api": api}
		with patch.object(dockside, "_apply_picker_action") as apply:
			dockside._apply_context_action([target], [container], "start", executor, log)
		apply.assert_called_once_with(api, [container], "start", executor, log)
		with pytest.raises(DockerError, match="No active Docker target"):
			dockside._apply_context_action([], [container], "start", executor, log)


	def test_context_grouping_and_state_restore(self) -> None:
		containers = [
			{"Id": "1", "_DocksideContext": "a"},
			{"Id": "2", "_DocksideContext": "b"},
		]
		assert set(dockside._group_context_containers(containers)) == {"a", "b"}
		state = PickerState(active_name="old", selected_names={"1", "2"})
		refreshed, active, selected = dockside._merge_container_refresh(
			containers, [{"Id": "2", "_DocksideContext": "b"}, {"Id": "3", "_DocksideContext": "b"}], 0, {"1"}
		)
		assert active == 0
		assert selected == set()
		assert len(refreshed) == 2
		assert dockside._restore_picker_state(refreshed, state)[0] == 0
		assert state.selected_names == {"2"}


