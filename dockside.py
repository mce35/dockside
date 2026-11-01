#!/usr/bin/env python3
"""Interactive Docker container manager for Linux terminals."""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import termios
import tempfile
import time
import tty
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from typing import Any

from container_logs import _decode_container_logs, _show_container_logs, _show_multiple_container_logs
from container_models import ActionLog, ImagePickerState, PickerState
from container_ops import (
	_container_display_name,
    _container_name,
	_delete_container,
	_prepare_recreate,
	_run_background_action,
	_run_recreate_logged,
)
from docker_api import DockerAPI, DockerError
from image_registry import RegistryChecks
from terminal_colors import (
    ANSI_BLUE,
    ANSI_GREEN,
    ANSI_LIGHT_GREEN_BG,
    ANSI_RED,
    ANSI_RESET_BG,
    ANSI_RESET_FG,
    ANSI_YELLOW,
)

_SORT_COLUMNS = (
	"id",
	"status",
	"name",
	"context",
	"created",
	"ports",
	"freshness",
	"image",
)
_SORT_LABELS = {
	"id": "CONTAINER ID",
	"status": "STATUS",
	"name": "NAME",
	"context": "CONTEXT",
	"created": "CREATED",
	"ports": "PORTS",
	"freshness": "LATEST?",
	"image": "IMAGE",
}
_PORTS_COLUMN_WIDTH = 32
_IMAGE_SORT_COLUMNS = ("context", "name", "sha", "size", "created", "used")
_IMAGE_SORT_LABELS = {
	"context": "CONTEXT",
	"name": "NAME",
	"sha": "SHA",
	"size": "SIZE",
	"created": "CREATED",
	"used": "USED BY",
}


def _show_containers(
	containers: list[dict[str, Any]], selected_ids: set[str] | None = None
) -> None:
	if not containers:
		print("No containers found.")
		return
	selected_ids = selected_ids or set()
	containers.sort(key=lambda item: _container_name(item).casefold())
	print(f"{'#':>3}  {'*':1}  {'CONTAINER ID':12}  {'STATUS':20}  {'NAME':24}  IMAGE")
	for index, container in enumerate(containers, start=1):
		print(
			f"{index:>3}  {'*' if container['Id'] in selected_ids else ' ':1}  "
			f"{container['Id'][:12]:12}  "
			f"{container.get('Status', '')[:20]:20}  "
			f"{_container_name(container)[:24]:24}  {container.get('Image', '')}"
		)


def _read_picker_key() -> str:
	character = sys.stdin.read(1)
	if character == "\x1b":
		prefix = sys.stdin.read(1)
		if prefix == "O":
			return {
				"P": "container_view",
				"Q": "images",
				"H": "home",
				"F": "end",
			}.get(sys.stdin.read(1), "")
		if prefix != "[":
			return ""
		sequence = sys.stdin.read(1)
		arrow_key = {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
			sequence
		)
		if arrow_key:
			return arrow_key
		if sequence in {"H", "F"}:
			return {"H": "home", "F": "end"}[sequence]
		if sequence.isdigit():
			terminator = sys.stdin.read(1)
			if terminator.isdigit():
				sequence += terminator
				terminator = sys.stdin.read(1)
			if terminator == "~":
				return {
					"1": "home",
					"4": "end",
					"7": "home",
					"8": "end",
					"11": "container_view",
					"12": "images",
				}.get(sequence, "")
		return ""
	return {
		" ": "toggle",
		"\r": "confirm",
		"\n": "confirm",
		"q": "action_down",
		"\x11": "quit",
		"b": "back",
		"B": "back",
		"\x01": "all",
		"a": "action_up",
		"A": "all",
		"c": "contexts",
		"C": "clear",
		"i": "inspect",
		"i": "inspect",
		"l": "logs",
		"s": "sort_direction",
		"\x13": "stop",
		"r": "restart",
		"\x12": "recreate",
		"p": "pull",
		"\x04": "delete",
		"\x03": "quit",
		"": "quit",
	}.get(character, "")


def _paint_span(line: str, start: int, width: int, color: str) -> str:
	end = min(start + width, len(line))
	if start >= end:
		return line
	return f"{line[:start]}{color}{line[start:end]}{ANSI_RESET_FG}{line[end:]}"


def _action_line_color(line: str) -> str | None:
	message = line.partition("] ")[2] or line
	if message.startswith("ERROR"):
		return ANSI_RED
	if message.startswith(
		(
			"Started container ",
			"Stopped container ",
			"Restarted container ",
			"Deleted container ",
			"Image pull completed for container ",
			"Previous configuration restored for ",
			"Container log view closed.",
		)
	) or (message.startswith("Container ") and " recreated successfully." in message):
		return ANSI_GREEN
	return None


def _color_action_line(line: str, width: int) -> str:
	visible_line = line[:width]
	color = _action_line_color(visible_line)
	return f"{color}{visible_line}{ANSI_RESET_FG}" if color else visible_line


def _common_view_header(
	daemon_version: str,
	context_names: str,
	sockets: str,
	active_view: str,
	width: int,
) -> str:
	metadata = (
		f"daemon={daemon_version} | context={context_names} | socket={sockets}"
	)
	suffix_width = len(" | F1 containers | F2 images")
	metadata_width = max(0, width - suffix_width)
	if len(metadata) > metadata_width:
		metadata = (
			metadata[: max(0, metadata_width - 3)] + "..."
			if metadata_width >= 3
			else metadata[:metadata_width]
		)
	containers_tab = "F1 containers"
	images_tab = "F2 images"
	if active_view == "containers":
		containers_tab = f"\033[7m{containers_tab}\033[0m"
	else:
		images_tab = f"\033[7m{images_tab}\033[0m"
	return f"{metadata} | {containers_tab} | {images_tab}"


def _status_color(container: dict[str, Any]) -> str:
	status = container.get("Status", "").casefold()
	state = container.get("State", "").casefold()
	if "unhealthy" in status or state in {"exited", "dead"}:
		return ANSI_RED
	if state == "running" and "healthy" in status:
		return ANSI_GREEN
	if state == "running":
		return ANSI_GREEN
	if state == "restarting":
		return ANSI_YELLOW
	return ANSI_BLUE


def _image_label(container: dict[str, Any], freshness: str) -> str:
	image = container.get("Image", "")
	if freshness != "OUTDATED":
		return image
	current_image = container.get("ImageID") or image
	return f"Original: {image} (Current: {current_image})"


def _container_created_label(container: dict[str, Any]) -> str:
	created = container.get("Created")
	try:
		if isinstance(created, (int, float)):
			return datetime.fromtimestamp(created).astimezone().strftime("%Y-%m-%d")
		if isinstance(created, str):
			return _format_created_date(created)[:10]
	except (OSError, OverflowError, ValueError):
		pass
	return "Unavailable"


def _container_port_labels(container: dict[str, Any]) -> list[str]:
	ports = []
	for port in container.get("Ports") or []:
		private_port = port.get("PrivatePort")
		protocol = port.get("Type", "tcp")
		if private_port is None:
			continue
		if port.get("PublicPort") is not None:
			binding = f"{port['PublicPort']}->{private_port}/{protocol}"
			is_unpublished = False
		else:
			binding = f"{private_port}/{protocol}"
			is_unpublished = True
		ports.append((is_unpublished, binding))
	return [binding for _, binding in sorted(set(ports))]


def _container_ports(container: dict[str, Any]) -> str:
	return ", ".join(_container_port_labels(container))


def _container_ports_cell(container: dict[str, Any], width: int) -> str:
	labels = _container_port_labels(container)
	for visible_count in range(len(labels), 0, -1):
		visible = ", ".join(labels[:visible_count])
		remaining = len(labels) - visible_count
		if remaining:
			visible += f", +{remaining}"
		if len(visible) <= width:
			return visible
	if labels:
		return labels[0][: max(0, width - 3)] + "..."[:width]
	return ""


def _container_context(container: dict[str, Any]) -> str:
	return container.get("_DocksideContext", "default")


def _container_key(container: dict[str, Any]) -> str:
	return container.get("_DocksideKey", container["Id"])


def _group_context_containers(
	containers: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
	groups: dict[str, list[dict[str, Any]]] = {}
	for container in containers:
		groups.setdefault(_container_context(container), []).append(container)
	return groups


class ContextRegistryChecks:
	def __init__(self, targets: list[dict[str, Any]], executor: ThreadPoolExecutor) -> None:
		self.targets = targets
		self.executor = executor
		self.api = self
		self.checks = {
			target["name"]: RegistryChecks(target["api"], executor, {})
			for target in targets
		}

	def list_containers(self) -> list[dict[str, Any]]:
		containers = []
		for target in self.targets:
			for listed_container in target["api"].list_containers():
				container = dict(listed_container)
				container["_DocksideContext"] = target["name"]
				container["_DocksideKey"] = f"{target['name']}\0{container['Id']}"
				containers.append(container)
		return containers

	def update(self, containers: list[dict[str, Any]]) -> None:
		for context_name, group in _group_context_containers(containers).items():
			self.checks[context_name].update(group)

	def snapshot(
		self, containers: list[dict[str, Any]]
	) -> tuple[dict[str, str], str]:
		freshness: dict[str, str] = {}
		progress: list[str] = []
		for context_name, group in _group_context_containers(containers).items():
			context_freshness, context_progress = self.checks[context_name].snapshot(group)
			progress.append(f"{context_name}: {context_progress}")
			for container in group:
				freshness[_container_key(container)] = context_freshness.get(
					container["Id"], "UNKNOWN"
				)
		return freshness, "; ".join(progress)

	def invalidate(self, containers: list[dict[str, Any]]) -> None:
		for context_name, group in _group_context_containers(containers).items():
			self.checks[context_name].invalidate(group)


def _image_key(image: dict[str, Any]) -> str:
	return image.get("_DocksideKey", image.get("Id", ""))


def _image_context(image: dict[str, Any]) -> str:
	return image.get("_DocksideContext", "default")


def _image_name(image: dict[str, Any]) -> str:
	tags = sorted(
		tag
		for tag in image.get("RepoTags") or []
		if tag and tag != "<none>:<none>"
	)
	if tags:
		return ", ".join(tags)
	digests = sorted(image.get("RepoDigests") or [])
	return ", ".join(digests) if digests else "<untagged>"


def _image_created_value(image: dict[str, Any]) -> float:
	created = image.get("Created")
	if isinstance(created, (int, float)):
		return float(created)
	if isinstance(created, str):
		try:
			return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
		except ValueError:
			pass
	return 0.0


def _image_created_label(image: dict[str, Any]) -> str:
	created = image.get("Created")
	try:
		if isinstance(created, (int, float)):
			return datetime.fromtimestamp(created).astimezone().strftime("%Y-%m-%d")
		if isinstance(created, str):
			return _format_created_date(created)[:10]
	except (OSError, OverflowError, ValueError):
		pass
	return "Unavailable"


def _list_context_images(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
	images = []
	for target in targets:
		context_name = target["name"]
		api = target["api"]
		containers = api.list_containers()
		used_by_id: dict[str, set[str]] = {}
		used_by_tag: dict[str, set[str]] = {}
		for container in containers:
			container_display_name = _container_display_name(
				{**container, "_DocksideContext": context_name}
			)
			image_id = container.get("ImageID")
			if image_id:
				used_by_id.setdefault(image_id, set()).add(container_display_name)
			image_reference = container.get("Image")
			if image_reference:
				used_by_tag.setdefault(image_reference, set()).add(container_display_name)
		for listed_image in api.list_images():
			image = dict(listed_image)
			image_id = image.get("Id", "")
			tags = [tag for tag in image.get("RepoTags") or [] if tag != "<none>:<none>"]
			used_by = set(used_by_id.get(image_id, set()))
			for tag in tags:
				used_by.update(used_by_tag.get(tag, set()))
			image["_DocksideContext"] = context_name
			image["_DocksideKey"] = f"{context_name}\0{image_id}"
			image["_DocksideUsed"] = sorted(used_by)
			if _image_name(image) == "<untagged>" and not used_by:
				continue
			images.append(image)
	return images


def _run_image_delete(
	api: DockerAPI, image: dict[str, Any], action_log: ActionLog
) -> None:
	context_name = _image_context(image)
	image_name = _image_name(image)
	try:
		api.remove_image(image["Id"])
	except (DockerError, OSError) as error:
		action_log.append(
			f"ERROR deleting image {context_name}/{image_name}: {error}",
			refresh=True,
		)
	else:
		action_log.append(
			f"Deleted image {context_name}/{image_name}.", refresh=True
		)


def _format_bytes(value: Any) -> str:
	try:
		byte_count = int(value)
	except (TypeError, ValueError):
		return "Unavailable"
	if byte_count < 1024:
		return f"{byte_count:,} B"
	amount = float(byte_count)
	for unit in ("KiB", "MiB", "GiB", "TiB"):
		amount /= 1024
		if amount < 1024 or unit == "TiB":
			return f"{amount:.1f} {unit} ({byte_count:,} B)"
	return f"{byte_count:,} B"


def _format_created_date(created: str | None) -> str:
	if not created:
		return "Unavailable"
	try:
		created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
	except ValueError:
		return created
	if created_at.tzinfo:
		created_at = created_at.astimezone()
	return created_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


def _inspection_ports(
	container: dict[str, Any], inspection: dict[str, Any]
) -> list[str]:
	ports = container.get("Ports") or []
	if ports:
		formatted = []
		for port in ports:
			private_port = port.get("PrivatePort")
			protocol = port.get("Type", "tcp")
			if private_port is None:
				continue
			if port.get("PublicPort") is None:
				formatted.append(f"{private_port}/{protocol} (unpublished)")
			else:
				host_ip = port.get("IP", "")
				if ":" in host_ip:
					host_ip = f"[{host_ip}]"
				host = f"{host_ip}:" if host_ip else ""
				formatted.append(
					f"{host}{port['PublicPort']} -> {private_port}/{protocol}"
				)
		return sorted(set(formatted))

	network_settings = inspection.get("NetworkSettings") or {}
	port_bindings = network_settings.get("Ports") or {}
	formatted = []
	for private_port, bindings in sorted(port_bindings.items()):
		if not bindings:
			formatted.append(f"{private_port} (unpublished)")
			continue
		for binding in bindings:
			host_ip = binding.get("HostIp", "")
			if ":" in host_ip:
				host_ip = f"[{host_ip}]"
			host = f"{host_ip}:" if host_ip else ""
			formatted.append(f"{host}{binding.get('HostPort', '?')} -> {private_port}")
	return formatted


def _inspection_stat_lines(stats: dict[str, Any] | None) -> list[str]:
	if stats is None:
		return [
			"CPU: Unavailable",
			"RAM: Unavailable",
			"Network bytes (in/out): Unavailable",
			"I/O bytes (read/write): Unavailable",
		]

	cpu_stats = stats.get("cpu_stats") or {}
	previous_cpu_stats = stats.get("precpu_stats") or {}
	cpu_usage = cpu_stats.get("cpu_usage") or {}
	previous_cpu_usage = previous_cpu_stats.get("cpu_usage") or {}
	total_cpu = cpu_usage.get("total_usage")
	cpu_description = "Unavailable"
	if total_cpu is not None:
		cpu_seconds = int(total_cpu) / 1_000_000_000
		previous_total_cpu = previous_cpu_usage.get("total_usage")
		system_cpu = cpu_stats.get("system_cpu_usage")
		previous_system_cpu = previous_cpu_stats.get("system_cpu_usage")
		online_cpus = cpu_stats.get("online_cpus") or len(
			cpu_usage.get("percpu_usage") or []
		)
		if (
			previous_total_cpu is not None
			and system_cpu is not None
			and previous_system_cpu is not None
			and online_cpus
		):
			cpu_delta = int(total_cpu) - int(previous_total_cpu)
			system_delta = int(system_cpu) - int(previous_system_cpu)
			if cpu_delta >= 0 and system_delta > 0:
				cpu_percent = cpu_delta / system_delta * online_cpus * 100
				cpu_description = f"{cpu_percent:.1f}% ({cpu_seconds:.2f} CPU s total)"
			else:
				cpu_description = f"Unavailable ({cpu_seconds:.2f} CPU s total)"
		else:
			cpu_description = f"Unavailable ({cpu_seconds:.2f} CPU s total)"

	memory_stats = stats.get("memory_stats") or {}
	memory_usage = memory_stats.get("usage")
	memory_limit = memory_stats.get("limit")
	if memory_usage is None:
		memory_description = "Unavailable"
	else:
		memory_counters = memory_stats.get("stats") or {}
		cache = next(
			(
				memory_counters[key]
				for key in ("total_inactive_file", "inactive_file", "cache")
				if memory_counters.get(key) is not None
			),
			0,
		)
		used = max(0, int(memory_usage) - int(cache))
		memory_description = _format_bytes(used)
		if memory_limit:
			memory_description += (
				f" / {_format_bytes(memory_limit)} "
				f"({used / int(memory_limit) * 100:.1f}%)"
			)

	network_stats = stats.get("networks")
	if isinstance(network_stats, dict):
		received = sum(int(values.get("rx_bytes", 0)) for values in network_stats.values())
		sent = sum(int(values.get("tx_bytes", 0)) for values in network_stats.values())
		network_description = f"{_format_bytes(received)} / {_format_bytes(sent)}"
	else:
		network_description = "Unavailable"

	block_stats = stats.get("blkio_stats") or {}
	io_records = block_stats.get("io_service_bytes_recursive")
	if io_records is None:
		io_description = "Unavailable"
	else:
		io_totals = {"read": 0, "write": 0}
		for record in io_records:
			operation = str(record.get("op", "")).casefold()
			if operation in io_totals:
				io_totals[operation] += int(record.get("value", 0))
		io_description = (
			f"{_format_bytes(io_totals['read'])} / "
			f"{_format_bytes(io_totals['write'])}"
		)
	return [
		f"CPU: {cpu_description}",
		f"RAM: {memory_description}",
		f"Network bytes (in/out): {network_description}",
		f"I/O bytes (read/write): {io_description}",
	]


def _container_inspection_lines(
	container: dict[str, Any],
	inspection: dict[str, Any],
	stats: dict[str, Any] | None,
	stats_error: str | None = None,
) -> list[str]:
	config = inspection.get("Config") or {}
	state = inspection.get("State") or {}
	image_name = config.get("Image") or container.get("Image") or "Unavailable"
	image_id = inspection.get("Image") or container.get("ImageID") or "Unavailable"
	state_values = [state.get("Status", "unknown")]
	if state.get("Running") is not None:
		state_values.append(f"running={state['Running']}")
	if state.get("ExitCode") is not None:
		state_values.append(f"exit code={state['ExitCode']}")
	if state.get("StartedAt"):
		state_values.append(f"started={_format_created_date(state['StartedAt'])}")

	lines = [
		f"Container: {_container_name(container)}",
		f"Image: {image_name} ({image_id})",
		f"Created: {_format_created_date(inspection.get('Created'))}",
		f"State: {'; '.join(state_values)}",
		"",
		"Environment:",
	]
	environment = config.get("Env") or []
	lines.extend(f"  {entry}" for entry in environment)
	if not environment:
		lines.append("  (none)")

	lines.extend(["", "Volumes / mount points:"])
	mounts = inspection.get("Mounts") or []
	for mount in mounts:
		source = mount.get("Source") or mount.get("Name") or "?"
		destination = mount.get("Destination") or mount.get("Target") or "?"
		mode = "rw" if mount.get("RW", True) else "ro"
		lines.append(
			f"  {mount.get('Type', 'mount')}: {source} -> {destination} ({mode})"
		)
	if not mounts:
		lines.append("  (none)")

	lines.extend(["", "Ports:"])
	ports = _inspection_ports(container, inspection)
	lines.extend(f"  {port}" for port in ports)
	if not ports:
		lines.append("  (none)")

	lines.extend(["", "Networks:"])
	network_settings = inspection.get("NetworkSettings") or {}
	networks = network_settings.get("Networks") or {}
	for network_name, network in sorted(networks.items()):
		lines.append(f"  {network_name}:")
		lines.append(f"    IPv4: {network.get('IPAddress') or 'Unavailable'}")
		lines.append(
			f"    IPv6: {network.get('GlobalIPv6Address') or 'Unavailable'}"
		)
		lines.append(f"    MAC: {network.get('MacAddress') or 'Unavailable'}")
	if not networks:
		lines.append("  (none)")

	lines.extend(["", "Usage:", *_inspection_stat_lines(stats)])
	if stats_error:
		lines.append(f"Statistics unavailable: {stats_error}")
	return lines


def _show_container_inspection(api: DockerAPI, container: dict[str, Any]) -> None:
	inspection = api.inspect_container(container["Id"])
	stats_error = None
	try:
		stats = api.container_stats(container["Id"])
	except (DockerError, OSError) as error:
		stats = None
		stats_error = str(error)
	lines = _container_inspection_lines(container, inspection, stats, stats_error)
	pager = shutil.which("less")
	if not pager:
		raise DockerError("The 'less' pager is required to view container inspection.")
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as detail_file:
		detail_file.write("\n".join(lines) + "\n")
		detail_file.flush()
		subprocess.run([pager, "-R", detail_file.name], check=False)


def _sort_label(column: str, sort_column: str, sort_ascending: bool) -> str:
	label = _SORT_LABELS[column]
	if column == sort_column:
		return f"{label} {'↑' if sort_ascending else '↓'}"
	return label


def _sort_containers(
	containers: list[dict[str, Any]],
	sort_column: str,
	sort_ascending: bool,
	image_freshness: dict[str, str],
) -> None:
	def sort_value(container: dict[str, Any]) -> str:
		if sort_column == "id":
			return container.get("Id", "").casefold()
		if sort_column == "status":
			return container.get("Status", "").casefold()
		if sort_column == "context":
			return _container_context(container).casefold()
		if sort_column == "created":
			return _container_created_label(container).casefold()
		if sort_column == "ports":
			return _container_ports(container).casefold()
		if sort_column == "freshness":
			return image_freshness.get(_container_key(container), "UNKNOWN").casefold()
		if sort_column == "image":
			return container.get("Image", "").casefold()
		return _container_name(container).casefold()

	containers.sort(key=lambda container: _container_name(container).casefold())
	containers.sort(key=sort_value, reverse=not sort_ascending)


def _render_container_picker(
	containers: list[dict[str, Any]],
	active_index: int,
	selected_ids: set[str],
	image_freshness: dict[str, str],
	registry_progress: str,
	action_log: ActionLog,
	sort_column: str = "name",
	sort_ascending: bool = True,
	context_name: str | None = None,
	docker_endpoint: str | None = None,
	daemon_version: str | None = None,
) -> None:
	terminal_size = shutil.get_terminal_size((80, 24))
	columns = max(1, terminal_size.columns - 1)
	terminal_rows = max(1, terminal_size.lines)
	selection_label = f"{len(selected_ids)}✓"
	selection_width = max(4, len(selection_label))
	number_width = max(2, len(str(len(containers))))
	column_header = (
		f"{'':{number_width + 2}}  {selection_label:<{selection_width}}  "
		f"{_sort_label('id', sort_column, sort_ascending):12}  "
		f"{_sort_label('status', sort_column, sort_ascending):8}  "
		f"{_sort_label('name', sort_column, sort_ascending):16}  "
		f"{_sort_label('context', sort_column, sort_ascending):12}  "
		f"{_sort_label('created', sort_column, sort_ascending):12}  "
		f"{_sort_label('ports', sort_column, sort_ascending):{_PORTS_COLUMN_WIDTH}}  "
		f"{_sort_label('freshness', sort_column, sort_ascending):8}  "
		f"{_sort_label('image', sort_column, sort_ascending)}"
	)
	if terminal_rows >= 14:
		header_lines = ["", column_header]
		footer_lines = [
			registry_progress,
			"Ctrl+S: stop  r: start/restart  Ctrl+R: recreate  p: pull  Ctrl+D: delete",
			"F2: images  i: inspect  Up/Down move  Home/End first/last  Left/Right sort  s direction",
			"c: contexts  Ctrl+A: all  C: clear  a/q: action log  Ctrl+Q: quit",
		]
	elif terminal_rows >= 9:
		header_lines = ["", column_header]
		footer_lines = [
			f"F2: images  i: inspect  Home/End: first/last  sort: Left/Right"
		]
	elif terminal_rows >= 5:
		header_lines = ["", column_header]
		footer_lines = ["F2: images  Home/End: first/last  Left/Right: sort"]
	elif terminal_rows >= 2:
		header_lines = [column_header]
		footer_lines = []
	else:
		header_lines = []
		footer_lines = []
	has_action_pane = terminal_rows >= 5
	show_separators = has_action_pane and terminal_rows >= 7
	separator_count = 2 if show_separators else 0
	common_header_rows = int(
		bool(context_name or docker_endpoint or daemon_version)
	)
	action_line_count = (
		min(
			8,
			max(
				0,
				terminal_rows
				- common_header_rows
				- len(header_lines)
				- len(footer_lines)
				- 2
				- separator_count,
			),
		)
		if has_action_pane
		else 0
	)
	visible_count = max(
		1,
		terminal_rows
		- common_header_rows
		- len(header_lines)
		- len(footer_lines)
		- (action_line_count + 1 + separator_count if has_action_pane else 0),
	)
	if containers:
		max_start = max(0, len(containers) - visible_count)
		start_index = min(max(0, active_index - visible_count // 2), max_start)
		end_index = min(len(containers), start_index + visible_count)
		title = f"Docker containers (showing {start_index + 1}-{end_index} of {len(containers)})"
	else:
		start_index = 0
		end_index = 0
		title = "Docker containers (showing 0 of 0)"
	if terminal_rows >= 5:
		header_lines[0] = title
	common_header = (
		_common_view_header(
			daemon_version or "unknown",
			context_name or "unknown",
			docker_endpoint or "unknown",
			"containers",
			columns,
		)
		if context_name or docker_endpoint or daemon_version
		else None
	)
	lines = ([common_header] if common_header else []) + [
		line[:columns] for line in header_lines
	]
	for index in range(start_index, end_index):
		container = containers[index]
		container_key = _container_key(container)
		marker = "[✓]" if container_key in selected_ids else "[ ]"
		active_marker = ">" if index == active_index else " "
		prefix = (
			f"{active_marker} {index + 1:<{number_width}}  "
			f"{marker:<{selection_width}}  "
			f"{container['Id'][:12]:12}  "
		)
		status = container.get("Status", "")[:8]
		name = _container_name(container)[:16]
		context = _container_context(container)[:12]
		created = _container_created_label(container)
		ports = _container_ports_cell(container, _PORTS_COLUMN_WIDTH)
		freshness = image_freshness.get(container_key, "UNKNOWN")
		image = _image_label(container, freshness)
		freshness_start = (
			len(prefix) + 8 + 2 + 16 + 2 + 12 + 2 + 12 + 2 + _PORTS_COLUMN_WIDTH + 2
		)
		line = (
			f"{prefix}{status:8}  {name:16}  {context:12}  {created:12}  "
			f"{ports:{_PORTS_COLUMN_WIDTH}}  "
			f"{freshness:8}  "
			f"{image}"
		)[:columns]
		freshness_color = {
			"CURRENT": ANSI_GREEN,
			"OUTDATED": ANSI_RED,
			"UNKNOWN": ANSI_BLUE,
		}.get(freshness, ANSI_BLUE)
		line = _paint_span(line, freshness_start, 8, freshness_color)
		line = _paint_span(line, len(prefix), 8, _status_color(container))
		if container_key in selected_ids:
			line = f"{ANSI_LIGHT_GREEN_BG}{line.ljust(columns)}{ANSI_RESET_BG}"
			if index == active_index:
				line = f"\033[4m{line}\033[24m"
		elif index == active_index:
			line = f"\033[7m{line}\033[0m"
		lines.append(line)
	if show_separators:
		lines.append("-" * columns)
	if has_action_pane:
		action_lines, action_start, action_total = action_log.visible(action_line_count)
		if action_total:
			action_label = (
				f"Actions ({action_start + 1}-{action_start + len(action_lines)} "
				f"of {action_total}; a older, q newer)"
			)
		else:
			action_label = "Actions (0 entries; a older, q newer)"
		lines.append(action_label[:columns])
		if not action_total and action_line_count:
			lines.append("No actions yet."[:columns])
		lines.extend(_color_action_line(line, columns) for line in action_lines)
	if show_separators:
		lines.append("-" * columns)
	lines.extend(line[:columns] for line in footer_lines)
	sys.stdout.write("\033[2J\033[H")
	sys.stdout.write("\r\n".join(lines))
	sys.stdout.flush()


def _image_sort_value(image: dict[str, Any], column: str) -> Any:
	if column == "context":
		return _image_context(image).casefold()
	if column == "name":
		return _image_name(image).casefold()
	if column == "sha":
		return image.get("Id", "").casefold()
	if column == "size":
		return int(image.get("Size") or 0)
	if column == "created":
		return _image_created_value(image)
	return ", ".join(image.get("_DocksideUsed") or []).casefold()


def _sort_images(images: list[dict[str, Any]], state: ImagePickerState) -> None:
	images.sort(key=lambda image: _image_name(image).casefold())
	images.sort(
		key=lambda image: _image_sort_value(image, state.sort_column),
		reverse=not state.sort_ascending,
	)


def _image_column_label(column: str, state: ImagePickerState, width: int) -> str:
	label = _IMAGE_SORT_LABELS[column]
	if column == state.sort_column:
		label += " " + ("↑" if state.sort_ascending else "↓")
	if column == "size":
		return label[:width].rjust(width)
	return label[:width].ljust(width)


def _truncate_cell(value: str, width: int, *, align_right: bool = False) -> str:
	if len(value) <= width:
		return value.rjust(width) if align_right else value.ljust(width)
	if width <= 3:
		return value[:width]
	truncated = value[: width - 3] + "..."
	return truncated.rjust(width) if align_right else truncated


def _format_image_size(value: Any) -> str:
	return _format_bytes(value).split(" (", 1)[0]


def _render_image_picker(
	images: list[dict[str, Any]],
	active_index: int,
	selected_keys: set[str],
	state: ImagePickerState,
	action_log: ActionLog,
	context_names: str,
	sockets: str,
	daemon_version: str,
) -> None:
	terminal_size = shutil.get_terminal_size((80, 24))
	columns = max(1, terminal_size.columns - 1)
	rows = max(1, terminal_size.lines)
	context_width = min(12, max(6, columns // 12))
	name_width = min(24, max(12, columns // 6))
	sha_width = min(20, max(12, columns // 8))
	size_width = 10
	created_width = 10
	number_width = max(2, len(str(len(images))))
	used_width = min(
		48,
		max(
			6,
			columns
			- 5
			- 10
			- context_width
			- name_width
			- sha_width
			- size_width
			- created_width,
		),
	)
	selection_label = f"{len(selected_keys)}✓"
	selection_width = max(4, len(selection_label))
	number_width = max(2, len(str(len(images))))
	column_header = (
		f"{'':{number_width + 2}}  {selection_label:<{selection_width}}  "
		f"{_image_column_label('context', state, context_width)}  "
		f"{_image_column_label('name', state, name_width)}  "
		f"{_image_column_label('sha', state, sha_width)}  "
		f"{_image_column_label('size', state, size_width)}  "
		f"{_image_column_label('created', state, created_width)}  "
		f"{_image_column_label('used', state, used_width)}"
	)
	has_action_pane = rows >= 7
	footer_lines = (
		[
			"Up/Down move  Space select  Ctrl+A all  C clear  Left/Right sort  s direction",
			"a/q action log  Ctrl+D delete  F1 containers  F2 images  Ctrl+Q quit",
		]
		if rows >= 9
		else ["a/q action log  Ctrl+D delete  F1 containers  F2 images  Ctrl+Q quit"]
		if rows >= 5
		else []
	)
	separator_count = 1 if has_action_pane else 0
	max_action_lines = (
		max(
			0,
			min(
				6,
				rows
				- 3
				- len(footer_lines)
				- separator_count
				- 1
				- 1,
			),
		)
		if has_action_pane
		else 0
	)
	action_lines, action_start, action_total = action_log.visible(max_action_lines)
	action_display_count = max(1, len(action_lines)) if has_action_pane else 0
	title = "Docker images (showing 0 of 0)"
	if images:
		active_index = max(0, min(active_index, len(images) - 1))
		visible_count = max(
			1,
			rows
			- 3
			- len(footer_lines)
			- separator_count
			- (1 + action_display_count if has_action_pane else 0),
		)
		start = min(max(0, active_index - visible_count // 2), max(0, len(images) - visible_count))
		end = min(len(images), start + visible_count)
		title = f"Docker images (showing {start + 1}-{end} of {len(images)})"
	else:
		start = 0
		end = 0
	common_header = _common_view_header(
		daemon_version, context_names, sockets, "images", columns
	)
	lines = [common_header, title[:columns], column_header[:columns]]
	for index in range(start, end):
		image = images[index]
		key = _image_key(image)
		marker = "[✓]" if key in selected_keys else "[ ]"
		active_marker = ">" if index == active_index else " "
		used = ", ".join(image.get("_DocksideUsed") or []) or "(none)"
		fields = [
			_truncate_cell(_image_context(image), context_width),
			_truncate_cell(_image_name(image), name_width),
			_truncate_cell(image.get("Id", "Unavailable"), sha_width),
			_truncate_cell(
				_format_image_size(image.get("Size")), size_width, align_right=True
			),
			_truncate_cell(_image_created_label(image), created_width),
			_truncate_cell(used, used_width),
		]
		line = (
			f"{active_marker} {index + 1:<{number_width}}  "
			f"{marker:<{selection_width}}  "
			+ "  ".join(fields)
		)
		if key in selected_keys:
			line = f"{ANSI_LIGHT_GREEN_BG}{line}{ANSI_RESET_BG}"
			if index == active_index:
				line = f"\033[4m{line}\033[24m"
		elif index == active_index:
			line = f"\033[7m{line[:columns]}\033[0m"
		lines.append(line[:columns])
	if has_action_pane:
		lines.append("-" * columns)
		if action_total:
			if action_lines:
				action_label = (
					f"Actions ({action_start + 1}-{action_start + len(action_lines)} "
					f"of {action_total}; a older, q newer)"
				)
			else:
				action_label = f"Actions ({action_total} entries; a older, q newer)"
		else:
			action_label = "Actions (0 entries; a older, q newer)"
		lines.append(action_label[:columns])
		if action_total:
			lines.extend(_color_action_line(line, columns) for line in action_lines)
		else:
			lines.append("No actions yet."[:columns])
	lines.extend(line[:columns] for line in footer_lines)
	sys.stdout.write("\033[2J\033[H" + "\r\n".join(lines))
	sys.stdout.flush()


def _select_images(
	targets: list[dict[str, Any]],
	state: ImagePickerState,
	action_log: ActionLog,
	executor: ThreadPoolExecutor,
) -> str:
	images = _list_context_images(targets)
	state.selected_keys.intersection_update(_image_key(image) for image in images)
	if state.active_key not in {_image_key(image) for image in images}:
		state.active_key = _image_key(images[0]) if images else None
	pending_deletes: dict[str, Future[None]] = {}
	file_descriptor = sys.stdin.fileno()
	original_terminal = termios.tcgetattr(file_descriptor)
	try:
		tty.setraw(file_descriptor)
		while True:
			completed_deletes = [
				image_key
				for image_key, future in pending_deletes.items()
				if future.done()
			]
			if completed_deletes:
				for image_key in completed_deletes:
					pending_deletes.pop(image_key, None)
				images = _list_context_images(targets)
				state.selected_keys.intersection_update(
					_image_key(image) for image in images
				)
				if state.active_key not in {_image_key(image) for image in images}:
					state.active_key = _image_key(images[0]) if images else None
			_sort_images(images, state)
			active_index = next(
				(
					index
					for index, image in enumerate(images)
					if _image_key(image) == state.active_key
				),
				0,
			)
			_render_image_picker(
				images,
				active_index,
				state.selected_keys,
				state,
				action_log,
				",".join(target["name"] for target in targets),
				",".join(target["endpoint"] for target in targets),
				",".join(
					dict.fromkeys(str(target["api"].daemon_version) for target in targets)
				),
			)
			readable, _, _ = select.select([file_descriptor], [], [], 0.2)
			if not readable:
				continue
			key = _read_picker_key()
			if key == "container_view":
				return "containers"
			if key == "quit":
				return "quit"
			if key == "up":
				active_index = max(0, active_index - 1)
				if images:
					state.active_key = _image_key(images[active_index])
			elif key == "home":
				active_index = 0
				if images:
					state.active_key = _image_key(images[active_index])
			elif key == "down":
				active_index = min(max(0, len(images) - 1), active_index + 1)
				if images:
					state.active_key = _image_key(images[active_index])
			elif key == "end":
				active_index = max(0, len(images) - 1)
				if images:
					state.active_key = _image_key(images[active_index])
			elif key in {"left", "right"}:
				column_index = _IMAGE_SORT_COLUMNS.index(state.sort_column)
				step = -1 if key == "left" else 1
				state.sort_column = _IMAGE_SORT_COLUMNS[
					(column_index + step) % len(_IMAGE_SORT_COLUMNS)
				]
			elif key == "sort_direction":
				state.sort_ascending = not state.sort_ascending
			elif key == "action_up":
				action_log.scroll(1)
			elif key == "action_down":
				action_log.scroll(-1)
			elif key == "toggle" and images:
				image_key = _image_key(images[active_index])
				if image_key in state.selected_keys:
					state.selected_keys.remove(image_key)
				else:
					state.selected_keys.add(image_key)
			elif key == "all":
				state.selected_keys = {_image_key(image) for image in images}
			elif key == "clear":
				state.selected_keys.clear()
			elif key == "delete" and images:
				selected_keys = state.selected_keys or {
					_image_key(images[active_index])
				}
				selected_images = [
					image
					for image in images
					if _image_key(image) in selected_keys
					and _image_key(image) not in pending_deletes
				]
				if not selected_images:
					action_log.append("Selected images are already being deleted.")
					continue
				names = [
					f"{_image_context(image)}/{_image_name(image)}"
					for image in selected_images
				]
				prompt = (
					f"Delete image {names[0]}?"
					if len(names) == 1
					else f"Delete {len(names)} selected images?"
				)
				if _confirm_dialog(prompt, names, list_label="Images") is not True:
					action_log.append("Image deletion cancelled.")
					continue
				for image in selected_images:
					context_name = _image_context(image)
					image_name = _image_name(image)
					action_log.append(
						f"Deleting image {context_name}/{image_name} in the background."
					)
					api = next(
						target["api"]
						for target in targets
						if target["name"] == context_name
					)
					pending_deletes[_image_key(image)] = executor.submit(
						_run_image_delete, api, image, action_log
					)
					state.selected_keys.discard(_image_key(image))
			elif images:
				state.active_key = _image_key(images[active_index])
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\033[0m\n")
		sys.stdout.flush()


def _merge_container_refresh(
	current: list[dict[str, Any]],
	refreshed: list[dict[str, Any]],
	active_index: int,
	selected_ids: set[str],
) -> tuple[list[dict[str, Any]], int, set[str]]:
	active_name = (
		_container_key(current[active_index])
		if current and 0 <= active_index < len(current)
		else None
	)
	selected_names = {
		_container_key(container)
		for container in current
		if _container_key(container) in selected_ids
	}
	refreshed.sort(key=lambda item: _container_key(item))
	selected_ids = {
		_container_key(container)
		for container in refreshed
		if _container_key(container) in selected_names
	}
	if active_name is not None and any(
		_container_key(container) == active_name for container in refreshed
	):
		active_index = next(
			index
			for index, container in enumerate(refreshed)
			if _container_key(container) == active_name
		)
	else:
		active_index = min(active_index, max(0, len(refreshed) - 1))
	return refreshed, active_index, selected_ids


def _restore_picker_state(
	containers: list[dict[str, Any]], state: PickerState
) -> tuple[int, set[str]]:
	keys = {_container_key(container) for container in containers}
	state.selected_names.intersection_update(keys)
	if not containers:
		state.active_name = None
		state.active_index = 0
		return 0, set()
	active_index = next(
		(
			index
			for index, container in enumerate(containers)
			if _container_key(container) == state.active_name
		),
		min(state.active_index, len(containers) - 1),
	)
	state.active_index = active_index
	state.active_name = _container_key(containers[active_index])
	selected_ids = {
		_container_key(container)
		for container in containers
		if _container_key(container) in state.selected_names
	}
	return active_index, selected_ids


def _select_containers(
	containers: list[dict[str, Any]],
	registry_checks: ContextRegistryChecks,
	picker_state: PickerState,
	action_log: ActionLog,
	last_action_refresh: int,
	context_name: str | None = None,
	docker_endpoint: str | None = None,
	daemon_version: str | None = None,
) -> tuple[list[dict[str, Any]], str | None] | None:
	if not sys.stdin.isatty():
		raise DockerError("Container selection requires an interactive terminal.")

	_sort_containers(
		containers,
		picker_state.sort_column,
		picker_state.sort_ascending,
		{},
	)
	active_index, selected_ids = _restore_picker_state(containers, picker_state)
	refresh_future: Future[list[dict[str, Any]]] | None = None
	next_refresh = time.monotonic() + 10
	file_descriptor = sys.stdin.fileno()
	original_terminal = termios.tcgetattr(file_descriptor)
	try:
		tty.setraw(file_descriptor)
		while True:
			if refresh_future is not None and refresh_future.done():
				try:
					refreshed_containers = refresh_future.result()
				except Exception as error:
					registry_progress = f"Container refresh failed: {error}"
				else:
					containers, active_index, selected_ids = _merge_container_refresh(
							containers, refreshed_containers, active_index, selected_ids
					)
					registry_checks.update(containers)
					registry_progress = "Container list refreshed."
				refresh_future = None
				next_refresh = time.monotonic() + 10
			action_refresh_due = (
				action_log.completion_generation() > last_action_refresh
			)
			if refresh_future is None and (
				action_refresh_due or time.monotonic() >= next_refresh
			):
				last_action_refresh = action_log.completion_generation()
				refresh_future = registry_checks.executor.submit(
					registry_checks.api.list_containers
				)
			registry_checks.update(containers)
			image_freshness, registry_progress = registry_checks.snapshot(containers)
			active_container_id = (
				_container_key(containers[active_index]) if containers else None
			)
			_sort_containers(
				containers,
				picker_state.sort_column,
				picker_state.sort_ascending,
				image_freshness,
			)
			if active_container_id:
				active_index = next(
					index
					for index, container in enumerate(containers)
					if _container_key(container) == active_container_id
				)
			picker_state.active_index = active_index
			picker_state.active_name = (
				_container_key(containers[active_index]) if containers else None
			)
			picker_state.selected_names = {
				_container_key(container)
				for container in containers
				if _container_key(container) in selected_ids
			}
			if refresh_future is not None and not refresh_future.done():
				registry_progress += "; container refresh in progress"
			_render_container_picker(
				containers,
				active_index,
				selected_ids,
				image_freshness,
				registry_progress,
				action_log,
				picker_state.sort_column,
				picker_state.sort_ascending,
				context_name,
				docker_endpoint,
				daemon_version,
			)
			wait_time = min(0.2, max(0, next_refresh - time.monotonic()))
			readable, _, _ = select.select([file_descriptor], [], [], wait_time)
			if not readable:
				continue
			key = _read_picker_key()
			if not containers:
				if key == "quit":
					return None
				if key == "images":
					return [], "images"
				if key == "contexts":
					return [], "contexts"
				continue
			if key == "up":
				active_index = max(0, active_index - 1)
			elif key == "home":
				active_index = 0
			elif key == "end":
				active_index = max(0, len(containers) - 1)
			elif key == "down":
				active_index = min(len(containers) - 1, active_index + 1)
			elif key in {"left", "right"}:
				column_index = _SORT_COLUMNS.index(picker_state.sort_column)
				column_step = -1 if key == "left" else 1
				picker_state.sort_column = _SORT_COLUMNS[
					(column_index + column_step) % len(_SORT_COLUMNS)
				]
			elif key == "sort_direction":
				picker_state.sort_ascending = not picker_state.sort_ascending
			elif key == "toggle":
				container_id = _container_key(containers[active_index])
				if container_id in selected_ids:
					selected_ids.remove(container_id)
				else:
					selected_ids.add(container_id)
			elif key == "all":
				selected_ids = {_container_key(container) for container in containers}
			elif key == "action_up":
				action_log.scroll(1)
			elif key == "action_down":
				action_log.scroll(-1)
			elif key == "clear":
				selected_ids.clear()
			elif key in {
				"contexts",
				"images",
				"inspect",
				"logs",
				"stop",
				"restart",
				"recreate",
				"pull",
				"delete",
			}:
				target_ids = selected_ids or {
					_container_key(containers[active_index])
				}
				return (
					[
						container
						for container in containers
						if _container_key(container) in target_ids
					],
					key,
				)
			elif key == "quit":
				return None
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\033[0m\n")
		sys.stdout.flush()


def _confirm_dialog(
	prompt: str,
	container_names: list[str],
	details: list[str] | None = None,
	list_label: str = "Containers",
) -> bool | None:
	selected_yes = True
	file_descriptor = sys.stdin.fileno()
	original_terminal = termios.tcgetattr(file_descriptor)
	columns = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
	container_lines = [f"  - {name}"[:columns] for name in container_names]
	try:
		tty.setraw(file_descriptor)
		while True:
			yes_label = "[ Yes ]" if selected_yes else "  Yes  "
			no_label = "  No  " if selected_yes else "[ No ]"
			if selected_yes:
				yes_label = f"\033[7m{yes_label}\033[0m"
			else:
				no_label = f"\033[7m{no_label}\033[0m"
			dialog_lines = [prompt[:columns], ""]
			if details:
				dialog_lines.extend(line[:columns] for line in details)
				dialog_lines.append("")
			dialog_lines.extend([f"{list_label}:", *container_lines, ""])
			dialog_lines.extend(
				[
					f"{yes_label}    {no_label}",
					"",
					"Left/Right: select   Enter: confirm   q: cancel",
				]
			)
			sys.stdout.write("\033[2J\033[H" + "\r\n".join(dialog_lines))
			sys.stdout.flush()
			character = os.read(file_descriptor, 1).decode(errors="replace")
			if character == "\x1b":
				readable, _, _ = select.select([file_descriptor], [], [], 0.1)
				if not readable:
					return None
				sequence = os.read(file_descriptor, 2)
				if sequence in {b"[D", b"OD"}:
					selected_yes = True
				elif sequence in {b"[C", b"OC"}:
					selected_yes = False
				continue
			if character in {"\r", "\n"}:
				return selected_yes
			if character in {"n", "N"}:
				selected_yes = False
			elif character in {"y", "Y"}:
				selected_yes = True
			elif character in {"q", "Q", "\x03", ""}:
				return None
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\033[0m\n")
		sys.stdout.flush()


def _confirm_delete(container_names: list[str]) -> bool:
	count_label = (
		f"Delete container {container_names[0]}?"
		if len(container_names) == 1
		else f"Delete {len(container_names)} selected containers?"
	)
	return _confirm_dialog(count_label, container_names) is True


def _recreate_context_batch(
	api_containers: list[tuple[DockerAPI, dict[str, Any]]],
	executor: ThreadPoolExecutor,
	action_log: ActionLog,
) -> None:
	plans: list[tuple[DockerAPI, Any, str]] = []
	preparation_failed = False
	for api, container in api_containers:
		display_name = _container_display_name(container)
		action_log.append(f"Preparing recreation of container {display_name}.")
		try:
			plan = _prepare_recreate(
				api, container["Id"], _container_context(container)
			)
		except (DockerError, OSError) as error:
			action_log.append(
				f"ERROR preparing {display_name} for recreation: {error}", refresh=True
			)
			preparation_failed = True
			continue
		plans.append((api, plan, display_name))
	if preparation_failed or len(plans) != len(api_containers):
		action_log.append(
			"Recreation cancelled; not all selected containers could be prepared.",
			refresh=True,
		)
		return

	container_names = [display_name for _, _, display_name in plans]
	image_details = [
		f"{display_name}: {plan.image}"
		for _, plan, display_name in plans
	]
	pull_decision = _confirm_dialog(
		"Pull all selected images before recreating?",
		container_names,
		image_details,
	)
	if pull_decision is None:
		action_log.append("Recreation cancelled for all selected containers.")
		return
	confirmation_details = [
		"All listed containers will be replaced.",
		"Configured mounts will be reused where available.",
	]
	auto_remove_names = [
		display_name
		for _, plan, display_name in plans
		if plan.auto_remove
	]
	if auto_remove_names:
		confirmation_details.append(
			"Auto-remove is enabled for: "
			+ ", ".join(auto_remove_names)
			+ "; anonymous volume data may be removed when stopped."
		)
	if _confirm_dialog(
		"Recreate all selected containers?", container_names, confirmation_details
	) is not True:
		action_log.append("Recreation cancelled for all selected containers.")
		return
	for api, plan, display_name in plans:
		plan.pull_first = pull_decision
		action_log.append(f"Recreating container {display_name} in the background.")
		executor.submit(_run_recreate_logged, api, plan, action_log)


def _apply_picker_action(
	api: DockerAPI,
	containers: list[dict[str, Any]],
	action: str,
	executor: ThreadPoolExecutor,
	action_log: ActionLog,
) -> None:
	if action == "delete":
		container_names = [
			_container_display_name(container) for container in containers
		]
		if not _confirm_delete(container_names):
			action_log.append("Deletion cancelled.")
			return
	if action == "logs":
		action_log.append("Opening selected container logs.")
		try:
			if len(containers) > 1:
				_show_multiple_container_logs(api, containers)
			else:
				_show_container_logs(api, containers[0]["Id"])
		except (DockerError, OSError) as error:
			action_log.append(f"ERROR displaying logs: {error}")
		else:
			action_log.append("Container log view closed.")
		return
	if action == "inspect":
		for container in containers:
			try:
				_show_container_inspection(api, container)
			except (DockerError, OSError) as error:
					action_log.append(
					f"ERROR inspecting {_container_display_name(container)}: {error}",
					refresh=True,
				)
		return
	if action == "recreate":
		_recreate_context_batch(
			[(api, container) for container in containers], executor, action_log
		)
		return
	start_messages = {
		"start": "Starting",
		"stop": "Stopping",
		"restart": "Restarting",
		"pull": "Pulling image for",
		"delete": "Deleting",
	}
	for container in containers:
		name = _container_display_name(container)
		action_log.append(f"{start_messages[action]} container {name}.")
		executor.submit(_run_background_action, api, container, action, action_log)


def _docker_host_endpoint() -> str:
	return os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")


def _docker_contexts() -> list[dict[str, Any]]:
	try:
		result = subprocess.run(
			["docker", "context", "ls", "--format", "{{json .}}"],
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired) as error:
		raise DockerError(f"Could not list Docker contexts: {error}") from error
	if result.returncode:
		detail = result.stderr.strip() or "Docker context list failed."
		raise DockerError(detail)
	try:
		contexts = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
	except json.JSONDecodeError as error:
		raise DockerError("Docker returned an invalid context list.") from error
	return [
		context
		for context in contexts
		if context.get("Name") and context.get("DockerEndpoint")
	]


def _comma_separated_values(argument: str, option: str) -> list[str]:
	values = [value.strip() for value in argument.split(",")]
	if not values or any(not value for value in values):
		raise DockerError(f"{option} expects non-empty comma-separated values.")
	return values


def _pick_docker_context(
	contexts: list[dict[str, Any]], current_context_names: set[str]
) -> list[dict[str, Any]] | None:
	if not contexts:
		return None
	terminal_size = shutil.get_terminal_size((80, 24))
	columns = max(1, terminal_size.columns - 1)
	selected_contexts = {
		index
		for index, context in enumerate(contexts)
		if context.get("Name") in current_context_names
	}
	if not selected_contexts:
		selected_contexts = {
			index for index, context in enumerate(contexts) if context.get("Current")
		}
	selected_index = min(selected_contexts, default=0)
	file_descriptor = sys.stdin.fileno()
	original_terminal = termios.tcgetattr(file_descriptor)
	try:
		tty.setraw(file_descriptor)
		while True:
			lines = ["Docker contexts (Space toggles; Enter applies)", ""]
			for index, context in enumerate(contexts):
				current = " (current)" if context.get("Current") else ""
				endpoint = context.get("DockerEndpoint", "")
				checked = "[x]" if index in selected_contexts else "[ ]"
				line = f"{'>' if index == selected_index else ' '} {checked} {context['Name']}{current}: {endpoint}"
				if context.get("Error"):
					line += f" ({context['Error']})"
				line = line[:columns]
				if index == selected_index:
					line = f"\033[7m{line.ljust(columns)}\033[0m"
				lines.append(line)
			lines.extend(["", "Up/Down: move   Space: toggle   Enter: apply   q: cancel"])
			sys.stdout.write("\033[2J\033[H" + "\r\n".join(lines))
			sys.stdout.flush()
			key = _read_picker_key()
			if key == "up":
				selected_index = max(0, selected_index - 1)
			elif key == "home":
				selected_index = 0
			elif key == "down":
				selected_index = min(len(contexts) - 1, selected_index + 1)
			elif key == "end":
				selected_index = len(contexts) - 1
			elif key == "toggle":
				if selected_index in selected_contexts:
					selected_contexts.remove(selected_index)
				else:
					selected_contexts.add(selected_index)
			elif key == "confirm":
				if selected_contexts:
					return [contexts[index] for index in sorted(selected_contexts)]
				return None
			elif key in {"quit", "action_down", "back"}:
				return None
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\033[0m\n")
		sys.stdout.flush()


def _targets_from_contexts(
	contexts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	return [
		{
			"name": context["Name"],
			"endpoint": context["DockerEndpoint"],
			"api": DockerAPI(context["DockerEndpoint"]),
		}
		for context in contexts
	]


def _apply_context_action(
	targets: list[dict[str, Any]],
	containers: list[dict[str, Any]],
	action: str,
	executor: ThreadPoolExecutor,
	action_log: ActionLog,
) -> None:
	target_apis = {target["name"]: target["api"] for target in targets}
	groups = _group_context_containers(containers)
	for context_name in groups:
		if context_name not in target_apis:
			raise DockerError(f"No active Docker target for context {context_name}.")
	api_containers = [
		(target_apis[context_name], container)
		for context_name, group in groups.items()
		for container in group
	]
	if action == "recreate":
		_recreate_context_batch(api_containers, executor, action_log)
		return
	if action == "delete":
		container_names = [
			_container_display_name(container)
			for _, container in api_containers
		]
		if not _confirm_delete(container_names):
			action_log.append("Deletion cancelled.")
			return
		for api, container in api_containers:
			name = _container_display_name(container)
			action_log.append(f"Deleting container {name}.")
			executor.submit(
				_run_background_action, api, container, action, action_log
			)
		return
	for context_name, group in groups.items():
		_apply_picker_action(
			target_apis[context_name], group, action, executor, action_log
		)


def _socket_targets(socket_argument: str) -> list[dict[str, Any]]:
	endpoints = _comma_separated_values(socket_argument, "--socket")
	return [
		{
			"name": f"socket{index + 1}",
			"endpoint": endpoint,
			"api": DockerAPI(endpoint),
		}
		for index, endpoint in enumerate(endpoints)
	]


def _initial_docker_targets(
	contexts_argument: str | None,
	socket_argument: str | None,
) -> list[dict[str, Any]]:
	if socket_argument:
		return _socket_targets(socket_argument)
	if contexts_argument:
		if not shutil.which("docker"):
			raise DockerError("--contexts requires the Docker CLI.")
		all_contexts = {context["Name"]: context for context in _docker_contexts()}
		requested_names = _comma_separated_values(contexts_argument, "--contexts")
		missing_names = [name for name in requested_names if name not in all_contexts]
		if missing_names:
			raise DockerError(
				"Docker context not found: " + ", ".join(missing_names)
			)
		return _targets_from_contexts(
			[all_contexts[name] for name in dict.fromkeys(requested_names)]
		)
	if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
		return _socket_targets(_docker_host_endpoint())
	if not shutil.which("docker"):
		if os.environ.get("DOCKER_CONTEXT"):
			raise DockerError("DOCKER_CONTEXT requires the Docker CLI.")
		return _socket_targets(_docker_host_endpoint())
	contexts = _docker_contexts()
	context_name = os.environ.get("DOCKER_CONTEXT")
	context = next((item for item in contexts if item.get("Name") == context_name), None)
	if context is None:
		context = next((item for item in contexts if item.get("Current")), None)
	if context is None:
		return _socket_targets(_docker_host_endpoint())
	return _targets_from_contexts([context])


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	connection_options = parser.add_mutually_exclusive_group()
	connection_options.add_argument(
		"--contexts",
		"--context",
		dest="contexts",
		help="comma-separated Docker contexts to display (--context is an alias)",
	)
	connection_options.add_argument(
		"--socket",
		help="comma-separated Unix socket paths or SSH endpoints",
	)
	arguments = parser.parse_args()

	try:
		targets = _initial_docker_targets(arguments.contexts, arguments.socket)
		with ThreadPoolExecutor(max_workers=8) as executor:
			registry_checks = ContextRegistryChecks(targets, executor)
			picker_state = PickerState()
			image_picker_state = ImagePickerState()
			action_log = ActionLog()
			while True:
				last_action_refresh = action_log.completion_generation()
				containers = registry_checks.api.list_containers()
				registry_checks.update(containers)
				context_title = ",".join(target["name"] for target in targets)
				endpoint_title = ",".join(target["endpoint"] for target in targets)
				daemon_title = ",".join(
					dict.fromkeys(str(target["api"].daemon_version) for target in targets)
				)
				selection = _select_containers(
					containers,
					registry_checks,
					picker_state,
					action_log,
					last_action_refresh,
					context_title,
					endpoint_title,
					daemon_title,
				)
				if selection is None:
					return 0
				selected_containers, shortcut_action = selection
				if shortcut_action == "images":
					view = _select_images(
						targets, image_picker_state, action_log, executor
					)
					if view == "quit":
						return 0
					continue
				if shortcut_action == "contexts":
					try:
						contexts = _docker_contexts()
						selected_contexts = _pick_docker_context(
							contexts, {target["name"] for target in targets}
						)
						if selected_contexts is None:
							continue
						new_targets = _targets_from_contexts(selected_contexts)
					except (DockerError, OSError, KeyError) as error:
						action_log.append(f"ERROR switching Docker context: {error}", refresh=True)
						continue
					targets = new_targets
					registry_checks = ContextRegistryChecks(targets, executor)
					picker_state.active_name = None
					picker_state.active_index = 0
					picker_state.selected_names.clear()
					action_log.append(
						"Switched to Docker contexts "
						+ ", ".join(target["name"] for target in targets)
						+ ".",
						refresh=True,
					)
					continue
				if shortcut_action:
					_apply_context_action(
						targets,
						selected_containers,
						shortcut_action,
						executor,
						action_log,
					)
					if shortcut_action in {"pull", "recreate"}:
						registry_checks.invalidate(selected_containers)
					continue
	except (DockerError, OSError, KeyError) as error:
		print(f"Error: {error}", file=sys.stderr)
		return 1
	except (EOFError, KeyboardInterrupt):
		print("\nGoodbye.")
		return 0


if __name__ == "__main__":
	raise SystemExit(main())
