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

class TestDocksideHelperTests:
	def test_image_delete_and_inspection_edge_cases(self) -> None:
		log = ActionLog()
		api = Mock()
		image = {"Id": "sha", "RepoTags": ["repo:tag"], "_DocksideContext": "ctx"}
		dockside._run_image_delete(api, image, log)
		assert "Deleted image ctx/repo:tag" in log.entries[-1]
		api.remove_image.side_effect = DockerError("in use")
		dockside._run_image_delete(api, image, log)
		assert "ERROR deleting image" in log.entries[-1]
		ports = dockside._inspection_ports({"Ports": [
			{"PrivatePort": None}, {"PrivatePort": 80},
			{"PrivatePort": 81, "PublicPort": 8081, "IP": "127.0.0.1"},
			{"PrivatePort": 82, "PublicPort": 8082, "IP": "127.0.0.2"},
		]}, {})
		assert ports == ["127.0.0.1:8081 -> 81/tcp", "127.0.0.2:8082 -> 82/tcp", "80/tcp (unpublished)"]
		assert dockside._format_created_date("2024-02-03T04:05:06Z")[:10] == "2024-02-03"
		stats = dockside._inspection_stat_lines({"cpu_stats": {"cpu_usage": {"total_usage": 10}}, "memory_stats": {}, "blkio_stats": {}})
		assert "Unavailable" in stats[0] and stats[1] == "RAM: Unavailable"
		assert "Unavailable" in stats[2] and "Unavailable" in stats[3]
		invalid_delta = dockside._inspection_stat_lines({
			"cpu_stats": {"cpu_usage": {"total_usage": 10}, "system_cpu_usage": 1},
			"precpu_stats": {"cpu_usage": {"total_usage": 20}, "system_cpu_usage": 1},
		})
		assert "Unavailable (0.00 CPU s total)" in invalid_delta[0]


	def test_inspection_detail_empty_and_populated_sections(self) -> None:
		container = {"Names": ["/web"], "Image": "fallback", "Ports": []}
		lines = dockside._container_inspection_lines(container, {
			"State": {"Status": "running", "Running": True, "ExitCode": 0,
				"StartedAt": "2024-01-01T00:00:00Z"},
			"Mounts": [{"Type": "volume", "Name": "data", "Target": "/data", "RW": False}],
			"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "1.2.3.4"}}},
		}, None, "stats denied")
		joined = "\n".join(lines)
		assert "started=2024-01-01" in joined
		assert "volume: data -> /data (ro)" in joined
		assert "bridge:" in joined and "1.2.3.4" in joined
		assert "Statistics unavailable: stats denied" in joined
		assert "Environment:\n  (none)" in joined


	def test_port_cells_image_values_and_container_refresh_helpers(self) -> None:
		container = {"Ports": [
			{"PrivatePort": 80, "Type": "tcp", "PublicPort": 8080},
			{"PrivatePort": 443, "Type": "tcp"},
			{"PrivatePort": None},
		]}
		assert dockside._container_port_labels(container) == ["8080->80/tcp", "443/tcp"]
		assert dockside._container_ports(container) == "8080->80/tcp, 443/tcp"
		assert dockside._container_ports_cell(container, 12).endswith("...")
		assert dockside._container_ports_cell({}, 5) == ""
		assert dockside._container_ports_cell(container, 40) == "8080->80/tcp, 443/tcp"
		image = {"RepoTags": ["z:tag", "a:tag", "<none>:<none>"], "Created": "2024-01-01T00:00:00Z"}
		assert dockside._image_name(image) == "a:tag, z:tag"
		assert dockside._image_created_value(image) > 0
		assert dockside._image_created_label(image) != "Unavailable"
		assert dockside._image_name({"RepoDigests": ["repo@sha"]}) == "repo@sha"
		assert dockside._image_name({}) == "<untagged>"
		assert dockside._image_created_value({"Created": "broken"}) == 0
		assert dockside._image_created_value({}) == 0
		assert dockside._image_created_label({"Created": 10**100}) == "Unavailable"
		current = [{"Id": "a"}, {"Id": "b"}]
		refreshed, active, selected = dockside._merge_container_refresh(current, [{"Id": "b"}, {"Id": "c"}], 1, {"a", "b"})
		assert [item["Id"] for item in refreshed] == ["b", "c"] and active == 0 and selected == {"b"}
		assert dockside._merge_container_refresh(current, [], 9, set())[1] == 0


	def test_list_context_views_and_format_helpers(self) -> None:
		api = Mock()
		api.list_containers.return_value = [
			{"Id": "c", "Names": ["/web"], "Image": "tag:latest", "ImageID": "sha", "Mounts": [{"Type": "volume", "Name": "db"}]},
		]
		api.list_images.return_value = [
			{"Id": "sha", "RepoTags": ["tag:latest"], "Size": 2048},
			{"Id": "unused", "RepoTags": ["<none>:<none>"]},
			{"Id": "dangling", "RepoTags": ["<none>:<none>"]},
		]
		api.list_volumes.return_value = [{"Name": "db"}, {"Name": "unused"}]
		target = {"name": "ctx", "api": api}
		images = dockside._list_context_images([target])
		assert len(images) == 1 and images[0]["_DocksideUsed"] == ["ctx/web"]
		volumes = dockside._list_context_volumes([target])
		assert volumes[0]["_DocksideUsedBy"] == ["ctx/web"]
		assert volumes[1]["_DocksideUsedBy"] == []
		assert dockside._format_bytes("bad") == "Unavailable"
		assert dockside._format_bytes(100) == "100 B"
		assert "KiB" in dockside._format_bytes(2048)
		assert dockside._format_bytes(1024**5).startswith("1,125,899") is False
		assert dockside._format_created_date(None) == "Unavailable"
		assert dockside._format_created_date("garbage") == "garbage"


	def test_container_inspection_fallback_ports_and_stat_shapes(self) -> None:
		assert dockside._inspection_ports({}, {"NetworkSettings": {"Ports": {
			"80/tcp": None, "443/tcp": [{"HostIp": "::1", "HostPort": "8443"}],
		}}}) == ["[::1]:8443 -> 443/tcp", "80/tcp (unpublished)"]
		stats = {"cpu_stats": {"cpu_usage": {"total_usage": 50, "percpu_usage": [1, 2]}, "system_cpu_usage": 100},
			"precpu_stats": {"cpu_usage": {"total_usage": 25}, "system_cpu_usage": 50},
			"memory_stats": {"usage": 10, "limit": 20},
			"networks": {"eth0": {"rx_bytes": 2, "tx_bytes": 3}},
			"blkio_stats": {"io_service_bytes_recursive": [{"op": "Read", "value": 7}, {"op": "Write", "value": 8}]}}
		lines = dockside._inspection_stat_lines(stats)
		assert any("CPU:" in line for line in lines)
		assert any("RAM:" in line for line in lines)
		assert any("2 B / 3 B" in line for line in lines)
		assert any("7 B / 8 B" in line for line in lines)
		assert "CPU: Unavailable" in dockside._inspection_stat_lines(None)
		branches = dockside._inspection_stat_lines({
			"cpu_stats": {"cpu_usage": {"total_usage": 100, "percpu_usage": [1]}, "system_cpu_usage": 10},
			"precpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 10},
			"memory_stats": {"usage": 10, "stats": {"inactive_file": 4}},
			"networks": None,
			"blkio_stats": {"io_service_bytes_recursive": [{"op": "Other", "value": 90}]},
		})
		assert "Unavailable (0.00 CPU s total)" in branches[0]
		assert branches[1] == "RAM: 6 B"
		assert branches[2].endswith("Unavailable") and branches[3].endswith("0 B / 0 B")
		assert dockside._format_created_date("2024-01-01 00:00:00") != "Unavailable"


	def test_inspection_lines_empty_sections_and_show_pager_failures(self, monkeypatch) -> None:
		lines = dockside._container_inspection_lines({"Id": "id"}, {}, {})
		joined = "\n".join(lines)
		assert "Volumes / mount points:\n  (none)" in joined
		assert "Ports:\n  (none)" in joined and "Networks:\n  (none)" in joined
		populated = dockside._container_inspection_lines({"Id": "id", "Ports": [{"PrivatePort": 80}]}, {}, {})
		assert "80/tcp (unpublished)" in "\n".join(populated)
		api = Mock()
		api.inspect_container.return_value = {}
		api.container_stats.side_effect = DockerError("stats unavailable")
		with patch.object(dockside.shutil, "which", return_value=None):
			with pytest.raises(DockerError, match="less' pager"):
				dockside._show_container_inspection(api, {"Id": "id"})
		monkeypatch.setattr(dockside.shutil, "which", lambda _: "/usr/bin/less")
		captured = []
		with patch.object(dockside.subprocess, "run", side_effect=lambda command, **kwargs: captured.append(Path(command[-1]).read_text())):
			dockside._show_container_inspection(api, {"Id": "id"})
		assert "Statistics unavailable: stats unavailable" in captured[0]


	def test_registry_checks_cover_pending_completion_failure_and_invalidation(self) -> None:
		class FutureStub:
			def __init__(self, value=None, error=None, done=True): self.value, self.error, self.is_done = value, error, done
			def done(self): return self.is_done
			def result(self):
				if self.error: raise self.error
				return self.value
			def cancel(self): return True
		class ExecutorStub:
			def __init__(self, future): self.future = future
			def submit(self, *args): return self.future
		api = Mock()
		container = {"Id": "c", "Image": "tag", "ImageID": "sha"}
		checks = image_registry.RegistryChecks(api, ExecutorStub(FutureStub(done=False)), {})
		checks.update([container, {"Id": "missing"}])
		fresh, progress = checks.snapshot([container, {"Id": "missing"}])
		assert fresh == {"c": "CHECKING", "missing": "UNKNOWN"} and "in progress" in progress
		checks.futures["tag", "sha"] = FutureStub(error=RuntimeError("failed"))
		assert checks.snapshot([container])[0]["c"] == "UNKNOWN"
		checks.futures["tag", "sha"] = FutureStub(done=False)
		checks.invalidate([container, {"Id": "missing"}])
		assert not checks.futures and not checks.cache
		checks.update([])
		assert "c" not in checks.container_keys


	def test_status_image_and_created_display_helpers(self) -> None:
		assert dockside._status_color({"State": "exited"}) == dockside.ANSI_RED
		assert dockside._status_color({"State": "running", "Status": "Up (healthy)"}) == dockside.ANSI_GREEN
		assert dockside._status_color({"State": "restarting"}) == dockside.ANSI_YELLOW
		assert dockside._status_color({"State": "created"}) == dockside.ANSI_BLUE
		assert dockside._image_label({"Image": "tag", "ImageID": "sha"}, "OUTDATED") == "Original: tag (Current: sha)"
		assert dockside._image_label({"Image": "tag"}, "FRESH") == "tag"
		assert dockside._container_created_label({"Created": 0}) != "Unavailable"
		assert dockside._container_created_label({"Created": 10**100}) == "Unavailable"


	def test_show_containers_empty_and_rows(self, capsys) -> None:
		dockside._show_containers([])
		assert "No containers found" in capsys.readouterr().out
		items = [
			{"Id": "b", "Names": ["/zeta"], "Status": "running", "Image": "img"},
			{"Id": "a", "Names": ["/alpha"], "Status": "stopped", "Image": "img2"},
		]
		dockside._show_containers(items, {"a"})
		output = capsys.readouterr().out
		assert output.index("alpha") < output.index("zeta")
		assert "*  a" in output


	def test_inspection_ports_handles_published_unpublished_and_ipv6_bindings(self) -> None:
		ports = dockside._inspection_ports(
			{"Ports": [
				{"PrivatePort": 80, "Type": "tcp"},
				{"PrivatePort": 443, "PublicPort": 8443, "IP": "::1", "Type": "tcp"},
			]},
			{},
		)
		assert ports == ["80/tcp (unpublished)", "[::1]:8443 -> 443/tcp"]
		fallback = dockside._inspection_ports({}, {"NetworkSettings": {"Ports": {
			"80/tcp": None, "443/tcp": [{"HostIp": "::", "HostPort": "8443"}],
		}}})
		assert fallback == ["[::]:8443 -> 443/tcp", "80/tcp (unpublished)"]


	def test_stats_and_inspection_lines_format_container_details(self) -> None:
		stats = {
			"cpu_stats": {"cpu_usage": {"total_usage": 2_000_000_000}, "system_cpu_usage": 200_000_000_000, "online_cpus": 2},
			"precpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000}, "system_cpu_usage": 100_000_000_000},
			"memory_stats": {"usage": 20, "limit": 100, "stats": {"cache": 5}},
			"networks": {"eth0": {"rx_bytes": 1024, "tx_bytes": 2048}},
			"blkio_stats": {"io_service_bytes_recursive": [
				{"op": "read", "value": 10}, {"op": "write", "value": 20}
			]},
		}
		stat_lines = dockside._inspection_stat_lines(stats)
		assert "CPU: 2.0%" in stat_lines[0]
		assert "15 B / 100 B (15.0%)" in stat_lines[1]
		assert "1.0 KiB (1,024 B) / 2.0 KiB (2,048 B)" in stat_lines[2]
		assert stat_lines[3] == "I/O bytes (read/write): 10 B / 20 B"
		lines = dockside._container_inspection_lines(
			{"Id": "abc", "Names": ["/db"], "Ports": []},
			{
				"Config": {"Image": "postgres:latest", "Env": ["A=B"]},
				"Image": "sha256:id", "Created": "2025-01-01T00:00:00Z",
				"State": {"Status": "running", "Running": True, "ExitCode": 0},
				"Mounts": [{"Type": "volume", "Name": "data", "Destination": "/var/lib", "RW": False}],
				"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.1.0.2"}}},
			}, stats, "stats warning",
		)
		joined = "\n".join(lines)
		assert "Container: db" in joined
		assert "volume: data -> /var/lib (ro)" in joined
		assert "bridge:" in joined and "172.1.0.2" in joined
		assert "Statistics unavailable: stats warning" in joined
		assert dockside._inspection_stat_lines(None)[0] == "CPU: Unavailable"


	def test_list_context_images_resolves_usage_and_hides_unused_dangling_images(self) -> None:
		api = Mock()
		api.list_containers.return_value = [
			{"Id": "container-id", "Names": ["/web"], "ImageID": "sha256:used", "Image": "repo:latest"}
		]
		api.list_images.return_value = [
			{"Id": "sha256:used", "RepoTags": ["repo:latest"], "Size": 100},
			{"Id": "sha256:dangling", "RepoTags": ["<none>:<none>"]},
			{"Id": "sha256:tagged", "RepoTags": ["other:tag"]},
		]
		images = dockside._list_context_images([{"name": "prod", "api": api}])
		assert len(images) == 2
		assert images[0]["_DocksideContext"] == "prod"
		assert images[0]["_DocksideUsed"] == ["prod/web"]
		assert images[1]["_DocksideUsed"] == []


	def test_list_context_volumes_maps_container_mounts(self) -> None:
		api = Mock()
		api.list_containers.return_value = [{
			"Id": "container-id", "Names": ["/db"],
			"Mounts": [{"Type": "volume", "Name": "db-data"}, {"Type": "bind", "Name": "ignored"}],
		}]
		api.list_volumes.return_value = [{"Name": "db-data"}, {"Name": "unused"}]
		volumes = dockside._list_context_volumes([{"name": "prod", "api": api}])
		assert volumes[0]["_DocksideUsedBy"] == ["prod/db"]
		assert volumes[1]["_DocksideUsedBy"] == []


	def test_image_sort_and_volume_keys(self) -> None:
		images = [{"Id": "b", "RepoTags": ["z:latest"]}, {"Id": "a", "RepoTags": ["a:latest"]}]
		state = ImagePickerState(sort_column="name")
		dockside._sort_images(images, state)
		assert [image["Id"] for image in images] == ["a", "b"]
		assert dockside._volume_key({"_DocksideKey": "ctx\\0name"}) == "ctx\\0name"


	def test_container_sort_and_formatters(self) -> None:
		containers = [{"Id": "b", "Names": ["/z"]}, {"Id": "a", "Names": ["/a"]}]
		dockside._sort_containers(containers, "name", True, {})
		assert [dockside._container_name(item) for item in containers] == ["a", "z"]
		assert dockside._format_bytes(1536) == "1.5 KiB (1,536 B)"
		assert dockside._format_created_date(None) == "Unavailable"
		assert dockside._image_name({"RepoTags": ["<none>:<none>"]}) == "<untagged>"


	def test_volume_delete_logs_success_and_failure(self) -> None:
		log = ActionLog()
		api = Mock()
		dockside._run_volume_delete(api, {"Name": "data", "_DocksideContext": "prod"}, log)
		assert "Deleted volume prod/data" in log.entries[-1]
		api.remove_volume.side_effect = DockerError("in use")
		dockside._run_volume_delete(api, {"Name": "data", "_DocksideContext": "prod"}, log)
		assert "ERROR deleting volume" in log.entries[-1]
		assert dockside._action_line_color(log.entries[-1]) == dockside.ANSI_RED


