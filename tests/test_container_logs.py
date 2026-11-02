import contextlib
import io
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import container_logs
from docker_api import DockerError

class TestDockerLogTests:
	@staticmethod
	def frame(stream: int, payload: bytes) -> bytes:
		return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload

	def test_decode_tty_logs_as_plain_text(self) -> None:
		assert container_logs._decode_container_logs(b"hello\xff", True) == "hello\ufffd"

	def test_decode_multiplexed_stdout_and_stderr(self) -> None:
		data = self.frame(1, b"out") + self.frame(2, b"err")
		assert container_logs._decode_container_logs(data, False) == "outerr"

	def test_decode_rejects_incomplete_or_malformed_frames(self) -> None:
		with pytest.raises(DockerError):
			container_logs._decode_container_logs(b"short", False)
		with pytest.raises(DockerError):
			container_logs._decode_container_logs(b"\x01bad\x00\x00\x00\x00", False)
		with pytest.raises(DockerError, match="malformed"):
			container_logs._decode_container_logs(bytes([1, 1, 0, 0, 0, 0, 0, 0]), False)
		with pytest.raises(DockerError, match="malformed"):
			container_logs._decode_container_logs(bytes([1, 0, 0, 0, 0, 0, 0, 4]) + b"x", False)

	def test_log_view_empty_data_and_missing_pager(self, monkeypatch, capsys) -> None:
		api = Mock()
		api.inspect_container.return_value = {"Config": {"Tty": True}}
		api.container_logs.return_value = b""
		monkeypatch.setattr(container_logs.shutil, "which", lambda _: "/usr/bin/less")
		container_logs._show_container_logs(api, "id")
		assert "No logs available" in capsys.readouterr().out
		with patch.object(container_logs.shutil, "which", return_value=None):
			with pytest.raises(DockerError, match="less' pager"):
				container_logs._show_container_logs(api, "id")
			with pytest.raises(DockerError, match="less' pager"):
				container_logs._show_multiple_container_logs(api, [])

	def test_multiple_logs_handles_empty_and_unparseable_lines(self, monkeypatch, capsys) -> None:
		api = Mock()
		api.inspect_container.return_value = {"Config": {"Tty": True}}
		api.container_logs.return_value = b""
		monkeypatch.setattr(container_logs.shutil, "which", lambda _: "/usr/bin/less")
		container_logs._show_multiple_container_logs(api, [{"Id": "id"}])
		assert "No logs available" in capsys.readouterr().out
		api.container_logs.return_value = b"not-a-time final"
		captured = []
		with patch("container_logs.subprocess.run", side_effect=lambda command, **kwargs: captured.append(Path(command[-1]).read_text())):
			container_logs._show_multiple_container_logs(api, [{"Id": "id", "Names": ["/web"]}])
		assert captured[0].endswith("not-a-time final\n")

	def test_show_container_logs_writes_decoded_logs_to_pager(self, monkeypatch) -> None:
		api = Mock()
		api.inspect_container.return_value = {"Config": {"Tty": False}}
		api.container_logs.return_value = self.frame(1, b"latest log")
		monkeypatch.setattr(container_logs.shutil, "which", lambda name: "/usr/bin/less")
		captured = []
		with patch("container_logs.subprocess.run") as run:
			run.side_effect = lambda command, **kwargs: captured.append(Path(command[-1]).read_text())
			container_logs._show_container_logs(api, "container-id")
		assert run.call_args.args[0][:2] == ["/usr/bin/less", "-R"]
		assert captured == ["latest log"]

	def test_show_multiple_container_logs_merges_timestamps(self, monkeypatch) -> None:
		api = Mock()
		api.inspect_container.return_value = {"Config": {"Tty": True}}
		api.container_logs.side_effect = [
			b"2025-01-01T00:00:02Z second\n",
			b"2025-01-01T00:00:01Z first\n",
		]
		monkeypatch.setattr(container_logs.shutil, "which", lambda name: "/usr/bin/less")
		captured = []
		with patch("container_logs.subprocess.run") as run:
			run.side_effect = lambda command, **kwargs: captured.append(Path(command[-1]).read_text())
			container_logs._show_multiple_container_logs(api, [
				{"Id": "one", "Names": ["/one"]}, {"Id": "two", "Names": ["/two"]}
			])
		content = captured[0]
		assert content.index("first") < content.index("second")
		assert "[one]" in content and "[two]" in content

