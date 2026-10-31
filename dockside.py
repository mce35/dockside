#!/usr/bin/env python3
"""Interactive Docker container manager for Linux terminals."""

from __future__ import annotations

import argparse
import os
import select
import shutil
import sys
import termios
import time
import tty
from concurrent.futures import Future, ThreadPoolExecutor
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
		return {"A": "up", "B": "down"}.get(sys.stdin.read(1), "")
	return {
		" ": "toggle",
		"\r": "confirm",
		"\n": "confirm",
		"q": "action_down",
		"Q": "quit",
		"b": "back",
		"B": "back",
		"a": "action_up",
		"A": "all",
		"c": "clear",
		"C": "clear",
		"l": "logs",
		"s": "start",
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


def _render_container_picker(
	containers: list[dict[str, Any]],
	active_index: int,
	selected_ids: set[str],
	image_freshness: dict[str, str],
	registry_progress: str,
	action_log: ActionLog,
) -> None:
	terminal_size = shutil.get_terminal_size((80, 24))
	columns = max(1, terminal_size.columns - 1)
	terminal_rows = max(1, terminal_size.lines)
	column_header = f"{'':3}  {' ':3}  {'CONTAINER ID':12}  {'STATUS':20}  {'NAME':24}  {'LATEST?':8}  IMAGE"
	if terminal_rows >= 14:
		header_lines = ["Select containers", column_header]
		footer_lines = [
			registry_progress,
			"Latest: remote registry  l: logs  s: start  Ctrl+S: stop  r: restart  Ctrl+R: recreate  p: pull  Ctrl+D: delete",
			"Up/Down: move  Space: toggle  A: all  c: clear  Enter: menus  a/q: action log  Q: quit",
		]
	elif terminal_rows >= 9:
		header_lines = ["Select containers"]
		footer_lines = [f"{registry_progress}  a/q: scroll action log  A: all  Q: quit"]
	elif terminal_rows >= 5:
		header_lines = ["Select containers"]
		footer_lines = ["a/q: scroll action log  Ctrl+D: delete"]
	elif terminal_rows >= 2:
		header_lines = [column_header]
		footer_lines = []
	else:
		header_lines = []
		footer_lines = []
	has_action_pane = terminal_rows >= 5
	action_line_count = (
		min(8, max(0, terminal_rows - len(header_lines) - len(footer_lines) - 2))
		if has_action_pane
		else 0
	)
	visible_count = max(
		1,
		terminal_rows
		- len(header_lines)
		- len(footer_lines)
		- (action_line_count + 1 if has_action_pane else 0),
	)
	if containers:
		max_start = max(0, len(containers) - visible_count)
		start_index = min(max(0, active_index - visible_count // 2), max_start)
		end_index = min(len(containers), start_index + visible_count)
		header_lines[0:1] = [
			f"Select containers ({len(selected_ids)} selected, "
			f"showing {start_index + 1}-{end_index} of {len(containers)})"
		] if header_lines else []
	else:
		start_index = 0
		end_index = 0
		if header_lines:
			header_lines[0] = "No containers currently found."
	lines = [line[:columns] for line in header_lines]
	for index in range(start_index, end_index):
		container = containers[index]
		marker = "[x]" if container["Id"] in selected_ids else "[ ]"
		prefix = f"{index + 1:>3}  {marker:3}  {container['Id'][:12]:12}  "
		status = container.get("Status", "")[:20]
		name = _container_name(container)[:24]
		freshness = image_freshness.get(container["Id"], "UNKNOWN")
		image = _image_label(container, freshness)
		freshness_start = len(prefix) + 20 + 2 + 24 + 2
		line = (
			f"{prefix}{status:20}  {name:24}  "
			f"{freshness:8}  "
			f"{image}"
		)[:columns]
		freshness_color = {
			"CURRENT": ANSI_GREEN,
			"OUTDATED": ANSI_RED,
			"UNKNOWN": ANSI_BLUE,
		}.get(freshness, ANSI_BLUE)
		line = _paint_span(line, freshness_start, 8, freshness_color)
		line = _paint_span(line, len(prefix), 20, _status_color(container))
		if container["Id"] in selected_ids:
			line = f"{ANSI_LIGHT_GREEN_BG}{line.ljust(columns)}{ANSI_RESET_BG}"
			if index == active_index:
				line = f"\033[4m{line}\033[24m"
		elif index == active_index:
			line = f"\033[7m{line}\033[0m"
		lines.append(line)
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
		lines.extend(line[:columns] for line in action_lines)
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
) -> tuple[list[dict[str, Any]], str | None] | None:
	if not sys.stdin.isatty():
		raise DockerError("Container selection requires an interactive terminal.")

	containers.sort(key=lambda item: _container_name(item).casefold())
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
			)
			wait_time = min(0.2, max(0, next_refresh - time.monotonic()))
			readable, _, _ = select.select([file_descriptor], [], [], wait_time)
			if not readable:
				continue
			key = _read_picker_key()
			if not containers:
				if key == "quit":
					return None
				continue
			if key == "up":
				active_index = max(0, active_index - 1)
			elif key == "down":
				active_index = min(len(containers) - 1, active_index + 1)
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
				if selected_ids:
					return (
						[
							container
							for container in containers
							if container["Id"] in selected_ids
						],
						None,
					)
			elif key in {"logs", "start", "stop", "restart", "recreate", "pull", "delete"}:
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


def _confirm_delete(container_names: list[str]) -> bool:
	selected_yes = True
	file_descriptor = sys.stdin.fileno()
	original_terminal = termios.tcgetattr(file_descriptor)
	columns = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
	count_label = (
		f"Delete container {container_names[0]}?"
		if len(container_names) == 1
		else f"Delete {len(container_names)} selected containers?"
	)
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
			dialog_lines = [count_label, "", "Containers:", *container_lines, ""]
			dialog_lines.extend(
				[
					f"{yes_label}    {no_label}",
					"",
					"Space/Tab: toggle   Enter: confirm   q: cancel",
				]
			)
			sys.stdout.write("\033[2J\033[H" + "\r\n".join(dialog_lines))
			sys.stdout.flush()
			character = sys.stdin.read(1)
			if character in {"\r", "\n"}:
				return selected_yes
			if character in {"n", "N"}:
				selected_yes = False
			elif character in {"y", "Y"}:
				selected_yes = True
			elif character in {" ", "\t"}:
				selected_yes = not selected_yes
			elif character in {"q", "Q", "\x03", "\x1b", ""}:
				return False
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\033[0m\n")
		sys.stdout.flush()


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
	if action == "recreate":
		for container in containers:
			name = _container_name(container)
			action_log.append(f"Preparing recreation of container {name}.")
			try:
				plan = _prepare_recreate(api, container["Id"])
			except (DockerError, OSError) as error:
				action_log.append(
					f"ERROR preparing {name} for recreation: {error}", refresh=True
				)
				continue
			if plan is None:
				action_log.append(f"Recreation cancelled for {name}.")
				continue
			action_log.append(f"Recreating container {name} in the background.")
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
		return "quit" if key == "action_down" else key
	finally:
		termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_terminal)
		sys.stdout.write("\r\n")
		sys.stdout.flush()


def _container_actions(container: dict[str, Any]) -> str:
	name = _container_name(container)
	print(f"\nSelected: {name} ({container.get('Status', 'unknown')})")
	actions = "s start, Ctrl+S stop, r restart, Ctrl+R recreate, p pull, l logs, Ctrl+D delete, b back, q quit"
	while True:
		action = _read_action_key(actions)
		if action in {"b", "back"}:
			return "back"
		if action in {"quit", "q"}:
			raise KeyboardInterrupt
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


def _socket_path(argument: str | None) -> str:
	if argument:
		return argument
	docker_host = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
	if not docker_host.startswith("unix://"):
		raise DockerError("Only Unix-socket Docker hosts are supported; use --socket to specify one.")
	return docker_host[len("unix://") :]


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"--socket",
		help="Docker Unix socket path (default: DOCKER_HOST or /var/run/docker.sock)",
	)
	arguments = parser.parse_args()

	try:
		api = DockerAPI(_socket_path(arguments.socket))
		with ThreadPoolExecutor(max_workers=8) as executor:
			registry_checks = RegistryChecks(api, executor, {})
			picker_state = PickerState()
			action_log = ActionLog()
			while True:
				last_action_refresh = action_log.completion_generation()
				containers = api.list_containers()
				if not containers:
					_show_containers(containers)
					return 0
				registry_checks.update(containers)
				selection = _select_containers(
					containers,
					registry_checks,
					picker_state,
					action_log,
					last_action_refresh,
				)
				if selection is None:
					return 0
				selected_containers, shortcut_action = selection
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
