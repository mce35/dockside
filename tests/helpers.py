from unittest.mock import Mock

import dockside


class FakeStdin:
	def fileno(self):
		return 77

	def isatty(self):
		return True


def mock_picker_terminal(monkeypatch, keys):
	monkeypatch.setattr(dockside.sys, "stdin", FakeStdin())
	monkeypatch.setattr(dockside.termios, "tcgetattr", lambda _: [1])
	monkeypatch.setattr(dockside.termios, "tcsetattr", lambda *args: None)
	monkeypatch.setattr(dockside.tty, "setraw", lambda _: None)
	monkeypatch.setattr(dockside.select, "select", lambda *args: ([77], [], []))
	monkeypatch.setattr(dockside, "_read_picker_key", Mock(side_effect=keys))
