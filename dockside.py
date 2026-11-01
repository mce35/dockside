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
from container_models import ActionLog, PickerState
from container_ops import (
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

_SORT_COLUMNS = ("id", "status", "name", "ports", "freshness", "image")
_SORT_LABELS = {
	"id": "CONTAINER ID",
	"status": "STATUS",
	"name": "NAME",
	"ports": "PORTS",
	"freshness": "LATEST?",
	"image": "IMAGE",
}
_PORTS_COLUMN_WIDTH = 32


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
		if sys.stdin.read(1) != "[":
			return ""
		return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(
			sys.stdin.read(1), ""
		)
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
		if sort_column == "ports":
			return _container_ports(container).casefold()
		if sort_column == "freshness":
			return image_freshness.get(container.get("Id", ""), "UNKNOWN").casefold()
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
	column_header = (
		f"{'':3}  {' ':3}  "
		f"{_sort_label('id', sort_column, sort_ascending):12}  "
		f"{_sort_label('status', sort_column, sort_ascending):8}  "
		f"{_sort_label('name', sort_column, sort_ascending):16}  "
		f"{_sort_label('ports', sort_column, sort_ascending):{_PORTS_COLUMN_WIDTH}}  "
		f"{_sort_label('freshness', sort_column, sort_ascending):8}  "
		f"{_sort_label('image', sort_column, sort_ascending)}"
	)
	if terminal_rows >= 14:
		header_lines = ["Select containers", column_header]
		footer_lines = [
			registry_progress,
			"Enter: actions  Ctrl+S: stop  r: start/restart  Ctrl+R: recreate  p: pull  Ctrl+D: delete",
			"i: inspect  Left/Right: sort column  s: direction  Up/Down: move",
			"c: contexts  Ctrl+A: all  C: clear  a/q: action log  Ctrl+Q: quit",
		]
	elif terminal_rows >= 9:
		header_lines = ["Select containers"]
		footer_lines = [
			f"c: contexts  i: inspect  sort: Left/Right  Ctrl+A: all  Ctrl+Q: quit"
		]
	elif terminal_rows >= 5:
		header_lines = ["Select containers"]
		footer_lines = ["c: contexts  i: inspect  Left/Right: sort  Ctrl+Q: quit"]
	elif terminal_rows >= 2:
		header_lines = [column_header]
		footer_lines = []
	else:
		header_lines = []
		footer_lines = []
	has_action_pane = terminal_rows >= 5
	show_separators = has_action_pane and terminal_rows >= 7
	separator_count = 2 if show_separators else 0
	action_line_count = (
		min(
			8,
			max(
				0,
				terminal_rows
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
		- len(header_lines)
		- len(footer_lines)
		- (action_line_count + 1 + separator_count if has_action_pane else 0),
	)
	if containers:
		max_start = max(0, len(containers) - visible_count)
		start_index = min(max(0, active_index - visible_count // 2), max_start)
		end_index = min(len(containers), start_index + visible_count)
		title = (
			f"Select containers ({len(selected_ids)} selected, "
			f"showing {start_index + 1}-{end_index} of {len(containers)})"
		)
	else:
		start_index = 0
		end_index = 0
		title = "No containers currently found."
	if context_name or docker_endpoint or daemon_version:
		title_parts = [f"{len(selected_ids)} selected"]
		if terminal_rows < 14:
			title_parts.append(
				f"sort={_sort_label(sort_column, sort_column, sort_ascending)}"
			)
		if context_name:
			title_parts.append(f"context={context_name}")
		if daemon_version:
			title_parts.append(f"daemon={daemon_version}")
		if docker_endpoint:
			socket_value = (
				docker_endpoint[len("unix://") :]
				if docker_endpoint.startswith("unix://")
				else docker_endpoint
			)
			socket_prefix = f"{' | '.join(title_parts)} | socket="
			available = max(0, columns - len(socket_prefix))
			if len(socket_value) > available:
				socket_value = (
					socket_value[: available - 3] + "..."
					if available >= 3
					else socket_value[:available]
				)
			title = socket_prefix + socket_value
		else:
			title = " | ".join(title_parts)
	else:
		if terminal_rows < 14:
			title += f"  {_sort_label(sort_column, sort_column, sort_ascending)}"
	if header_lines:
		header_lines[0] = title
	lines = [line[:columns] for line in header_lines]
	for index in range(start_index, end_index):
		container = containers[index]
		marker = "[x]" if container["Id"] in selected_ids else "[ ]"
		prefix = f"{index + 1:>3}  {marker:3}  {container['Id'][:12]:12}  "
		status = container.get("Status", "")[:8]
		name = _container_name(container)[:16]
		ports = _container_ports_cell(container, _PORTS_COLUMN_WIDTH)
		freshness = image_freshness.get(container["Id"], "UNKNOWN")
		image = _image_label(container, freshness)
		freshness_start = (
			len(prefix) + 8 + 2 + 16 + 2 + _PORTS_COLUMN_WIDTH + 2
		)
		line = (
			f"{prefix}{status:8}  {name:16}  "
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
		if container["Id"] in selected_ids:
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


def _merge_container_refresh(
	current: list[dict[str, Any]],
	refreshed: list[dict[str, Any]],
	active_index: int,
	selected_ids: set[str],
) -> tuple[list[dict[str, Any]], int, set[str]]:
	active_name = (
		_container_name(current[active_index])
		if current and 0 <= active_index < len(current)
		else None
	)
	selected_names = {
		_container_name(container)
		for container in current
		if container["Id"] in selected_ids
	}
	refreshed.sort(key=lambda item: _container_name(item).casefold())
	refreshed_names = {_container_name(container) for container in refreshed}
	selected_ids = {
		container["Id"]
		for container in refreshed
		if _container_name(container) in selected_names
	}
	if active_name in refreshed_names:
		active_index = next(
			index
			for index, container in enumerate(refreshed)
			if _container_name(container) == active_name
		)
	else:
		active_index = min(active_index, max(0, len(refreshed) - 1))
	return refreshed, active_index, selected_ids


def _restore_picker_state(
	containers: list[dict[str, Any]], state: PickerState
) -> tuple[int, set[str]]:
	names = {_container_name(container) for container in containers}
	state.selected_names.intersection_update(names)
	if not containers:
		state.active_name = None
		state.active_index = 0
		return 0, set()
	active_index = next(
		(
			index
			for index, container in enumerate(containers)
			if _container_name(container) == state.active_name
		),
		min(state.active_index, len(containers) - 1),
	)
	state.active_index = active_index
	state.active_name = _container_name(containers[active_index])
	selected_ids = {
		container["Id"]
		for container in containers
		if _container_name(container) in state.selected_names
	}
	return active_index, selected_ids


def _select_containers(
	containers: list[dict[str, Any]],
	registry_checks: RegistryChecks,
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
				containers[active_index]["Id"] if containers else None
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
					if container["Id"] == active_container_id
				)
			picker_state.active_index = active_index
			picker_state.active_name = (
				_container_name(containers[active_index]) if containers else None
			)
			picker_state.selected_names = {
				_container_name(container)
				for container in containers
				if container["Id"] in selected_ids
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
				if key == "contexts":
					return [], "contexts"
				continue
			if key == "up":
				active_index = max(0, active_index - 1)
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
				container_id = containers[active_index]["Id"]
				if container_id in selected_ids:
					selected_ids.remove(container_id)
				else:
					selected_ids.add(container_id)
			elif key == "all":
				selected_ids = {container["Id"] for container in containers}
			elif key == "action_up":
				action_log.scroll(1)
			elif key == "action_down":
				action_log.scroll(-1)
			elif key == "clear":
				selected_ids.clear()
			elif key == "confirm":
				target_ids = selected_ids or {containers[active_index]["Id"]}
				return (
					[
						container
						for container in containers
						if container["Id"] in target_ids
					],
					None,
				)
			elif key in {
				"contexts",
				"inspect",
				"logs",
				"stop",
				"restart",
				"recreate",
				"pull",
				"delete",
			}:
				target_ids = selected_ids or {containers[active_index]["Id"]}
				return (
					[
						container
						for container in containers
						if container["Id"] in target_ids
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
			dialog_lines.extend(["Containers:", *container_lines, ""])
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


def _apply_picker_action(
	api: DockerAPI,
	containers: list[dict[str, Any]],
	action: str,
	executor: ThreadPoolExecutor,
	action_log: ActionLog,
) -> None:
	if action == "delete":
		container_names = [_container_name(container) for container in containers]
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
					f"ERROR inspecting {_container_name(container)}: {error}",
					refresh=True,
				)
		return
	if action == "recreate":
		plans = []
		preparation_failed = False
		for container in containers:
			name = _container_name(container)
			action_log.append(f"Preparing recreation of container {name}.")
			try:
				plan = _prepare_recreate(api, container["Id"])
			except (DockerError, OSError) as error:
				action_log.append(
					f"ERROR preparing {name} for recreation: {error}", refresh=True
				)
				preparation_failed = True
				continue
			plans.append(plan)
		if preparation_failed or len(plans) != len(containers):
			action_log.append(
				"Recreation cancelled; not all selected containers could be prepared.",
				refresh=True,
			)
			return
		container_names = [plan.name for plan in plans]
		image_details = [f"{plan.name}: {plan.image}" for plan in plans]
		pull_decision = _confirm_dialog(
			"Pull all selected images before recreating?",
			container_names,
			image_details,
		)
		if pull_decision is None:
			action_log.append("Recreation cancelled for all selected containers.")
			return
		pull_first = pull_decision
		confirmation_details = [
			"All listed containers will be replaced.",
			"Configured mounts will be reused where available.",
		]
		auto_remove_names = [plan.name for plan in plans if plan.auto_remove]
		if auto_remove_names:
			confirmation_details.append(
				"Auto-remove is enabled for: "
				+ ", ".join(auto_remove_names)
				+ "; anonymous volume data may be removed when stopped."
			)
		if _confirm_dialog(
			"Recreate all selected containers?",
			container_names,
			confirmation_details,
		) is not True:
			action_log.append("Recreation cancelled for all selected containers.")
			return
		for plan in plans:
			plan.pull_first = pull_first
			action_log.append(f"Recreating container {plan.name} in the background.")
			executor.submit(_run_recreate_logged, api, plan, action_log)
		return
	start_messages = {
		"start": "Starting",
		"stop": "Stopping",
		"restart": "Restarting",
		"pull": "Pulling image for",
		"delete": "Deleting",
	}
	for container in containers:
		name = _container_name(container)
		action_log.append(f"{start_messages[action]} container {name}.")
		executor.submit(_run_background_action, api, container, action, action_log)


def _read_action_key(prompt: str) -> str:
	if not sys.stdin.isatty():
		return input(f"{prompt}: ").strip().casefold()
	file_descriptor = sys.stdin.fileno()
	original_terminal = termios.tcgetattr(file_descriptor)
	try:
		tty.setraw(file_descriptor)
		sys.stdout.write(f"{prompt}: ")
		sys.stdout.flush()
		key = _read_picker_key()
		if key == "action_down":
			return "back"
		if key == "sort_direction":
			return "start"
		return key
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\r\n")
		sys.stdout.flush()


def _container_actions(container: dict[str, Any]) -> str:
	name = _container_name(container)
	print(f"\nSelected: {name} ({container.get('Status', 'unknown')})")
	actions = "i inspect, s start, Ctrl+S stop, r start/restart, Ctrl+R recreate, p pull, l logs, Ctrl+D delete, b/q back, Ctrl+Q quit"
	while True:
		action = _read_action_key(actions)
		if action in {"b", "back"}:
			return "back"
		if action == "quit":
			raise KeyboardInterrupt
		if action == "inspect":
			return "inspect"
		if action == "recreate":
			return "recreate"
		if action in {"pull", "p"}:
			return "pull"
		if action in {"logs", "l"}:
			return "logs"
		if action == "delete":
			return "delete"
		if action in {"start", "stop", "restart"}:
			return action
		print("Choose one of the listed actions.")


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


def _pick_docker_context(
	contexts: list[dict[str, Any]], current_context_name: str | None
) -> dict[str, Any] | None:
	if not contexts:
		return None
	terminal_size = shutil.get_terminal_size((80, 24))
	columns = max(1, terminal_size.columns - 1)
	selected_index = next(
		(
			index
			for index, context in enumerate(contexts)
			if context.get("Name") == current_context_name
		),
		next(
			(index for index, context in enumerate(contexts) if context.get("Current")),
			0,
		),
	)
	file_descriptor = sys.stdin.fileno()
	original_terminal = termios.tcgetattr(file_descriptor)
	try:
		tty.setraw(file_descriptor)
		while True:
			lines = ["Docker contexts", ""]
			for index, context in enumerate(contexts):
				current = " (current)" if context.get("Current") else ""
				endpoint = context.get("DockerEndpoint", "")
				line = f"{'>' if index == selected_index else ' '} {context['Name']}{current}: {endpoint}"
				if context.get("Error"):
					line += f" ({context['Error']})"
				line = line[:columns]
				if index == selected_index:
					line = f"\033[7m{line.ljust(columns)}\033[0m"
				lines.append(line)
			lines.extend(["", "Up/Down: select   Enter: switch   q: cancel"])
			sys.stdout.write("\033[2J\033[H" + "\r\n".join(lines))
			sys.stdout.flush()
			key = _read_picker_key()
			if key == "up":
				selected_index = max(0, selected_index - 1)
			elif key == "down":
				selected_index = min(len(contexts) - 1, selected_index + 1)
			elif key == "confirm":
				return contexts[selected_index]
			elif key in {"quit", "action_down", "back"}:
				return None
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\033[0m\n")
		sys.stdout.flush()


def _initial_docker_api(
	context_argument: str | None,
	socket_argument: str | None,
) -> tuple[DockerAPI, str | None]:
	if socket_argument:
		return DockerAPI(socket_argument), None
	if context_argument:
		if not shutil.which("docker"):
			raise DockerError("--context requires the Docker CLI.")
		context = next(
			(
				item
				for item in _docker_contexts()
				if item.get("Name") == context_argument
			),
			None,
		)
		if context is None:
			raise DockerError(f"Docker context not found: {context_argument}")
		return DockerAPI(context["DockerEndpoint"]), context["Name"]
	if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
		return DockerAPI(_docker_host_endpoint()), None
	if not shutil.which("docker"):
		if os.environ.get("DOCKER_CONTEXT"):
			raise DockerError("DOCKER_CONTEXT requires the Docker CLI.")
		return DockerAPI(_docker_host_endpoint()), None
	contexts = _docker_contexts()
	context_name = os.environ.get("DOCKER_CONTEXT")
	context = next(
		(
			item
			for item in contexts
			if item.get("Name") == context_name
		),
		next((item for item in contexts if item.get("Current")), None),
	)
	if context is None:
		return DockerAPI(_docker_host_endpoint()), None
	return DockerAPI(context["DockerEndpoint"]), context["Name"]


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	connection_options = parser.add_mutually_exclusive_group()
	connection_options.add_argument(
		"--context", help="Docker context to use on startup"
	)
	connection_options.add_argument(
		"--socket",
		help="Unix socket path or SSH endpoint (for example, ssh://user@host)",
	)
	arguments = parser.parse_args()

	try:
		api, current_context_name = _initial_docker_api(
			arguments.context, arguments.socket
		)
		with ThreadPoolExecutor(max_workers=8) as executor:
			registry_checks = RegistryChecks(api, executor, {})
			picker_state = PickerState()
			action_log = ActionLog()
			while True:
				last_action_refresh = action_log.completion_generation()
				containers = api.list_containers()
				registry_checks.update(containers)
				selection = _select_containers(
					containers,
					registry_checks,
					picker_state,
					action_log,
					last_action_refresh,
					current_context_name,
					api.endpoint,
					api.daemon_version,
				)
				if selection is None:
					return 0
				selected_containers, shortcut_action = selection
				if shortcut_action == "contexts":
					try:
						contexts = _docker_contexts()
						context = _pick_docker_context(contexts, current_context_name)
						if context is None:
							continue
						new_api = DockerAPI(context["DockerEndpoint"])
					except (DockerError, OSError, KeyError) as error:
						action_log.append(f"ERROR switching Docker context: {error}", refresh=True)
						continue
					api = new_api
					current_context_name = context["Name"]
					registry_checks = RegistryChecks(api, executor, {})
					picker_state.active_name = None
					picker_state.active_index = 0
					picker_state.selected_names.clear()
					action_log.append(
						f"Switched to Docker context {current_context_name}.", refresh=True
					)
					continue
				if shortcut_action:
					_apply_picker_action(
						api, selected_containers, shortcut_action, executor, action_log
					)
					if shortcut_action in {"pull", "recreate"}:
						registry_checks.invalidate(selected_containers)
					continue
				for container in selected_containers:
					menu_action = _container_actions(container)
					if menu_action != "back":
						_apply_picker_action(
							api, [container], menu_action, executor, action_log
						)
					if menu_action in {"pull", "recreate"}:
						registry_checks.invalidate([container])
	except (DockerError, OSError, KeyError) as error:
		print(f"Error: {error}", file=sys.stderr)
		return 1
	except (EOFError, KeyboardInterrupt):
		print("\nGoodbye.")
		return 0


if __name__ == "__main__":
	raise SystemExit(main())
