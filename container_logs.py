from __future__ import annotations

import colorsys
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any

from container_ops import _container_name
from docker_api import DockerAPI, DockerError
from terminal_colors import ANSI_RESET_FG


def _decode_container_logs(log_data: bytes, tty_enabled: bool) -> str:
	if tty_enabled:
		return log_data.decode(errors="replace")

	output: list[bytes] = []
	offset = 0
	while offset < len(log_data):
		if offset + 8 > len(log_data):
			raise DockerError("Docker returned an incomplete log frame.")
		header = log_data[offset : offset + 8]
		stream_type = header[0]
		frame_size = int.from_bytes(header[4:8], "big")
		frame_start = offset + 8
		frame_end = frame_start + frame_size
		if stream_type not in {1, 2, 3} or header[1:4] != b"\x00\x00\x00" or frame_end > len(log_data):
			raise DockerError("Docker returned a malformed log frame.")
		output.append(log_data[frame_start:frame_end])
		offset = frame_end
	return b"".join(output).decode(errors="replace")


def _show_container_logs(api: DockerAPI, container_id: str) -> None:
	pager = shutil.which("less")
	if not pager:
		raise DockerError("The 'less' pager is required to view container logs.")
	inspection = api.inspect_container(container_id)
	log_data = api.container_logs(container_id)
	log_text = _decode_container_logs(
		log_data, bool((inspection.get("Config") or {}).get("Tty"))
	)
	if not log_text:
		print("No logs available for this container.")
		return
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as log_file:
		log_file.write(log_text)
		log_file.flush()
		subprocess.run([pager, "-R", "+G", log_file.name], check=False)


def _show_multiple_container_logs(
	api: DockerAPI, containers: list[dict[str, Any]]
) -> None:
	pager = shutil.which("less")
	if not pager:
		raise DockerError("The 'less' pager is required to view container logs.")
	log_records: list[tuple[datetime | None, int, str, str, str]] = []
	container_count = len(containers)
	sequence = 0
	for index, container in enumerate(containers):
		container_id = container["Id"]
		inspection = api.inspect_container(container_id)
		log_data = api.container_logs(container_id)
		log_text = _decode_container_logs(
			log_data, bool((inspection.get("Config") or {}).get("Tty"))
		)
		red, green, blue = colorsys.hsv_to_rgb(
			index / max(container_count, 1), 0.75, 1.0
		)
		prefix_color = f"\033[38;2;{int(red * 255)};{int(green * 255)};{int(blue * 255)}m"
		prefix = f"[{_container_name(container)}] "
		for log_line in log_text.splitlines(keepends=True):
			try:
				timestamp_text = log_line.split(None, 1)[0]
				timestamp = datetime.fromisoformat(
					timestamp_text.replace("Z", "+00:00")
				)
				if timestamp.tzinfo is None:
					timestamp = timestamp.replace(tzinfo=timezone.utc)
			except (IndexError, ValueError):
				timestamp = None
			log_records.append((timestamp, sequence, prefix_color, prefix, log_line))
			sequence += 1
	if not log_records:
		print("No logs available for the selected containers.")
		return
	latest_timestamp = datetime.max.replace(tzinfo=timezone.utc)
	log_records.sort(
		key=lambda record: (
			record[0] is None,
			record[0] or latest_timestamp,
			record[1],
		)
	)
	with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as log_file:
		for _, _, prefix_color, prefix, log_line in log_records:
			if not log_line.endswith(("\n", "\r")):
				log_line += "\n"
			log_file.write(f"{prefix_color}{prefix}{ANSI_RESET_FG}{log_line}")
		log_file.flush()
		subprocess.run([pager, "-R", "+G", log_file.name], check=False)
