import pytest
from unittest.mock import Mock

import container_ops
from container_models import ActionLog
from docker_api import DockerError

class TestContainerOperationTests:
	def test_preserve_mount_sources_skips_invalid_and_existing_bind_targets(self) -> None:
		inspection = {"Mounts": [
			{"Type": "bind", "Target": "/bind", "Source": "/host"},
			{"Type": "volume", "Target": "/vol", "Source": "named"},
			{"Type": "tmpfs", "Target": "/tmp"},
			{"Type": "volume", "Target": "/already", "Source": "old"},
			{"Type": "volume", "Source": "missing-target"},
			{"Type": "volume", "Target": "/nosource"},
		]}
		host = {"Binds": ["/another:/bind"], "Mounts": [{"Target": "/already"}, {"Target": "/missing"}, {"Source": "no-target"}]}
		container_ops._preserve_mount_sources(inspection, host)
		assert host["Mounts"][0]["Source"] == "old"
		assert {item.get("Target") for item in host["Mounts"]} == {"/already", "/missing", None, "/vol", "/nosource"}
		assert any(item.get("Target") == "/nosource" and "Source" not in item for item in host["Mounts"])

	def test_recreate_without_auto_remove_and_without_original_image(self) -> None:
		api = Mock(create_container=Mock(return_value={"Id": "new"}))
		plan = container_ops.RecreatePlan("old", "worker", "image", {}, {"Image": "image"}, True, True, False)
		container_ops._execute_recreate(api, plan, ActionLog())
		api.stop_container.assert_called_once_with("old")
		api.remove_container.assert_not_called()
		api.start_container.assert_called_once_with("new")
		api.create_container.side_effect = None
		api.create_container.reset_mock()
		api.create_container.return_value = {"Id": "new2"}
		plan.was_running = False
		plan.auto_remove = False
		container_ops._execute_recreate(api, plan, ActionLog())
		api.remove_container.assert_called_once_with("old")
		api.start_container.assert_called_once_with("new")
		plan.inspection = {}
		api.create_container.side_effect = [DockerError("create failed"), {"Id": "restored"}]
		api.create_container.reset_mock()
		container_ops._execute_recreate(api, plan, ActionLog())
		assert api.create_container.call_args_list[-1].args[1]["Image"] == "image"

	def test_recreate_restore_failure_and_logged_background_errors(self) -> None:
		api = Mock()
		api.create_container.side_effect = [DockerError("new failed"), OSError("restore failed")]
		plan = container_ops.RecreatePlan(
			container_id="old", name="web", image="new", inspection={"Image": "old-sha"},
			payload={"Image": "new"}, was_running=False, auto_remove=False, pull_first=False,
		)
		log = ActionLog()
		with pytest.raises(OSError, match="restore failed"):
			container_ops._execute_recreate(api, plan, log)
		assert any("ERROR restoring web" in entry for entry in log.entries)
		api.create_container.side_effect = DockerError("create still fails")
		container_ops._run_recreate_logged(api, plan, log)
		assert any("ERROR recreating web" in entry for entry in log.entries)

	def test_background_actions_success_skips_and_failures(self) -> None:
		api = Mock()
		log = ActionLog()
		container = {"Id": "id", "Names": ["/web"], "_DocksideContext": "prod", "State": "running"}
		container_ops._run_background_action(api, container, "start", log)
		assert "Skipped starting" in log.entries[-1]
		container_ops._run_background_action(api, {**container, "State": "exited"}, "start", log)
		api.start_container.assert_called_once_with("id")
		container_ops._run_background_action(api, container, "stop", log)
		api.stop_container.assert_called_once_with("id")
		container_ops._run_background_action(api, container, "restart", log)
		api.restart_container.assert_called_once_with("id")
		api.inspect_container.return_value = {"Config": {"Image": "repo:latest"}}
		container_ops._run_background_action(api, container, "pull", log)
		api.pull_image.assert_called_once_with("repo:latest")
		api.inspect_container.return_value = {"Config": {}}
		container_ops._run_background_action(api, container, "pull", log)
		assert "ERROR: prod/web" in log.entries[-1]
		api.inspect_container.return_value = {"State": {"Running": False}, "HostConfig": {}}
		container_ops._run_background_action(api, container, "delete", log)
		api.remove_container.assert_called_once_with("id")
		api.start_container.side_effect = DockerError("daemon unavailable")
		container_ops._run_background_action(api, {**container, "State": "exited"}, "restart", log)
		assert "ERROR: prod/web" in log.entries[-1]
		container_ops._run_background_action(api, container, "unknown", log)
		assert "ERROR: prod/web" in log.entries[-1]

	def test_create_payload_preserves_existing_mount_sources_without_mutating_inspection(self) -> None:
		inspection = {
			"Config": {"Image": "old", "Env": ["A=B"]},
			"HostConfig": {
				"Mounts": [{"Type": "volume", "Target": "/data"}],
				"Binds": ["/host:/config:ro"],
			},
			"Mounts": [
				{"Type": "volume", "Name": "data-vol", "Source": "data-vol", "Target": "/data", "RW": False},
				{"Type": "bind", "Source": "/host", "Destination": "/config", "Target": "/config"},
				{"Type": "bind", "Source": "/extra", "Destination": "/extra", "Target": "/extra", "Propagation": "rshared", "RW": True},
			],
			"NetworkSettings": {"Networks": {"app": {"Aliases": ["web"], "IPAddress": "10.0.0.2"}}},
		}
		payload = container_ops._create_payload(inspection, "new-image")
		assert payload["Image"] == "new-image"
		assert payload["HostConfig"]["Mounts"][0]["Source"] == "data-vol"
		assert payload["HostConfig"]["Mounts"][1]["Source"] == "/extra"
		assert payload["HostConfig"]["Mounts"][1]["ReadOnly"] == False
		assert payload["HostConfig"]["Mounts"][1]["BindOptions"] == {"Propagation": "rshared"}
		assert payload["NetworkingConfig"]["EndpointsConfig"]["app"] == {"Aliases": ["web"]}
		assert inspection["Config"]["Image"] == "old"
		assert "Source" not in inspection["HostConfig"]["Mounts"][0]

	def test_prepare_recreate_captures_state_and_context(self) -> None:
		api = Mock()
		api.inspect_container.return_value = {
			"Name": "/worker", "Config": {"Image": "repo:tag"},
			"State": {"Running": True}, "HostConfig": {"AutoRemove": True},
		}
		plan = container_ops._prepare_recreate(api, "abc", "prod")
		assert (plan.name, plan.image, plan.context_name) == ("worker", "repo:tag", "prod")
		assert plan.was_running
		assert plan.auto_remove
		assert plan.payload["Image"] == "repo:tag"

	def test_prepare_recreate_requires_an_image(self) -> None:
		api = Mock()
		api.inspect_container.return_value = {"Config": {}}
		with pytest.raises(DockerError):
			container_ops._prepare_recreate(api, "abc")

	def test_delete_container_stops_running_auto_remove_container_without_remove(self) -> None:
		api = Mock()
		api.inspect_container.return_value = {"State": {"Running": True}, "HostConfig": {"AutoRemove": True}}
		container_ops._delete_container(api, "abc")
		api.stop_container.assert_called_once_with("abc")
		api.remove_container.assert_not_called()

	def test_execute_recreate_pulls_replaces_and_restarts_running_container(self) -> None:
		api = Mock()
		api.create_container.return_value = {"Id": "replacement"}
		plan = container_ops.RecreatePlan(
			container_id="old", name="worker", image="repo:new", inspection={},
			payload={"Image": "repo:new"}, was_running=True, auto_remove=False,
			pull_first=True, context_name="prod",
		)
		log = ActionLog()
		container_ops._execute_recreate(api, plan, log)
		api.pull_image.assert_called_once_with("repo:new")
		api.stop_container.assert_called_once_with("old")
		api.remove_container.assert_called_once_with("old")
		api.create_container.assert_called_once_with("worker", {"Image": "repo:new"})
		api.start_container.assert_called_once_with("replacement")
		assert "Container prod/worker recreated successfully" in log.entries[-1]

	def test_execute_recreate_restores_old_image_if_creation_fails(self) -> None:
		api = Mock()
		api.create_container.side_effect = [DockerError("create failed"), {"Id": "restored"}]
		plan = container_ops.RecreatePlan(
			container_id="old", name="worker", image="repo:new",
			inspection={"Image": "sha256:old"}, payload={"Image": "repo:new"},
			was_running=True, auto_remove=False, pull_first=False,
		)
		log = ActionLog()
		container_ops._execute_recreate(api, plan, log)
		assert api.create_container.call_args_list[1].args[1]["Image"] == "sha256:old"
		api.start_container.assert_called_once_with("restored")
		assert "Previous configuration restored" in log.entries[-1]

	def test_background_stop_skips_stopped_container(self) -> None:
		api = Mock()
		log = ActionLog()
		container_ops._run_background_action(api, {"Id": "abc", "Names": ["/db"], "State": "exited"}, "stop", log)
		api.stop_container.assert_not_called()
		assert "Skipped stopping db" in log.entries[-1]

	def test_background_restart_starts_stopped_container(self) -> None:
		api = Mock()
		log = ActionLog()
		container_ops._run_background_action(api, {"Id": "abc", "Names": ["/db"], "State": "exited"}, "restart", log)
		api.start_container.assert_called_once_with("abc")
		assert "Restarted container db" in log.entries[-1]

