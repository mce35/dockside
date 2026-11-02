from container_models import ActionLog
import container_ops

class TestContainerModelTests:
	def test_action_log_tracks_refresh_and_scroll(self) -> None:
		log = ActionLog(max_entries=2)
		log.append("one")
		log.append("two", refresh=True)
		log.append("three")
		assert log.completion_generation() == 1
		assert log.visible(2)[0] == log.entries[-2:]
		assert len(log.entries) == 2

	def test_container_name_and_context_display(self) -> None:
		container = {"Id": "1234567890abcdef", "Names": ["/web"], "_DocksideContext": "prod"}
		assert container_ops._container_name(container) == "web"
		assert container_ops._container_display_name(container) == "prod/web"

