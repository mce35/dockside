import base64
import email.message
import io
import json
from unittest.mock import Mock, patch

import pytest
import docker_api
from docker_api import DockerAPI, DockerError

class TestDockerAPITests:
	def test_unix_connection_and_request_response_edge_cases(self) -> None:
		connection = docker_api.UnixHTTPConnection("/tmp/docker.sock")
		sock = Mock()
		with patch("docker_api.socket.socket", return_value=sock):
			connection.connect()
		sock.settimeout.assert_called_once_with(connection.timeout)
		sock.connect.assert_called_once_with("/tmp/docker.sock")
		api = DockerAPI.__new__(DockerAPI)
		api.api_version, api.socket_path = "1.45", "/unused"
		for body, expected in [(b"", None), (b"plain text", "plain text")]:
			conn = Mock()
			conn.getresponse.return_value = Mock(status=200, read=Mock(return_value=body))
			with patch("docker_api.UnixHTTPConnection", return_value=conn):
				assert api.request("GET", "/x") == expected
			conn.close.assert_called_once()
		conn = Mock()
		conn.getresponse.return_value = Mock(status=400, read=Mock(return_value=b"not json"))
		with patch("docker_api.UnixHTTPConnection", return_value=conn):
			with pytest.raises(DockerError, match="not json"):
				api.request("GET", "/x")
		conn.getresponse.return_value = Mock(status=200, read=Mock(return_value=b"raw"))
		with patch("docker_api.UnixHTTPConnection", return_value=conn):
			assert api.request("GET", "/x", raw_response=True) == b"raw"

	def test_ssh_ipv6_invalid_endpoint_and_running_process_cleanup(self) -> None:
		api = DockerAPI.__new__(DockerAPI)
		api.endpoint = "ssh:///missing"
		with pytest.raises(DockerError, match="Invalid SSH"):
			api._ssh_request("GET", "/version", None)
		api.endpoint = "ssh://user@[::1]:2222"
		stdin, stdout, stderr = io.BytesIO(), io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"), io.BytesIO()
		process = Mock(stdin=stdin, stdout=stdout, stderr=stderr)
		process.wait.return_value = 0
		process.poll.side_effect = [None, 0]
		with patch("docker_api.subprocess.Popen", return_value=process) as popen:
			assert api._ssh_request("GET", "/version", None) == (200, b"{}")
		assert "user@[::1]" in popen.call_args.args[0]
		assert popen.call_args.args[0][popen.call_args.args[0].index("-p") + 1] == "2222"
		process.kill.assert_called_once()
	def test_init_normalizes_unix_endpoints_and_rejects_unsupported_schemes(self) -> None:
		with patch.object(DockerAPI, "request", return_value={"ApiVersion": "1.45", "Version": "27.0"}):
			api = DockerAPI("/var/run/docker.sock")
		assert api.endpoint == "unix:///var/run/docker.sock"
		assert api.socket_path == "/var/run/docker.sock"
		assert api.daemon_version == "27.0"
		with pytest.raises(DockerError, match="TCP Docker contexts"):
			DockerAPI("tcp://localhost:2375")
		with pytest.raises(DockerError, match="Unsupported Docker endpoint"):
			DockerAPI("http://localhost")

	def test_list_containers_resolves_configured_and_image_tag_names(self) -> None:
		api = DockerAPI.__new__(DockerAPI)
		api.api_version, api.socket_path = "1.45", "/tmp/docker.sock"
		api.request = Mock(return_value=[
			{"Id": "one", "Image": "sha256:one", "ImageID": "sha256:one"},
			{"Id": "two", "Image": "sha256:two", "ImageID": "sha256:two"},
			{"Id": "three", "Image": "sha256:three", "ImageID": "sha256:three"},
			{"Id": "four", "Image": "repo:tag"},
		])
		api.inspect_container = Mock(side_effect=[
			{"Config": {"Image": "configured:one"}},
			{"Config": {}},
			{"Config": {"Image": "sha256:three"}},
		])
		api.inspect_image = Mock(side_effect=[{"RepoTags": ["resolved:two"]}, {"RepoTags": ["<none>:<none>"]}])
		containers = api.list_containers()
		assert [container["Image"] for container in containers] == [
			"configured:one", "resolved:two", "sha256:three", "repo:tag"
		]
		api.request.assert_called_once_with("GET", "/containers/json", query={"all": 1})

	@pytest.mark.parametrize(
		("method", "args", "expected"),
		[
			("inspect_container", ("a/b",), ("GET", "/containers/a%2Fb/json")),
			("container_stats", ("id",), ("GET", "/containers/id/stats")),
			("inspect_image", ("sha256:abc",), ("GET", "/images/sha256%3Aabc/json")),
			("list_images", (), ("GET", "/images/json")),
			("container_logs", ("id",), ("GET", "/containers/id/logs")),
			("start_container", ("id",), ("POST", "/containers/id/start")),
			("restart_container", ("id",), ("POST", "/containers/id/restart")),
			("stop_container", ("id",), ("POST", "/containers/id/stop")),
			("remove_container", ("id",), ("DELETE", "/containers/id")),
			("pull_image", ("repo:tag",), ("POST", "/images/create")),
			("create_container", ("worker", {"Image": "repo"}), ("POST", "/containers/create")),
		],
	)
	def test_endpoint_methods_delegate_to_engine_request(self, method, args, expected) -> None:
		api = DockerAPI.__new__(DockerAPI)
		api.api_version, api.socket_path = "1.45", "/tmp/docker.sock"
		with patch.object(api, "request", return_value={"Id": "created"}) as request:
			getattr(api, method)(*args)
		assert request.call_args.args == expected
		if method == "container_stats":
			assert request.call_args.kwargs["query"] == {"stream": 0, "one-shot": 1}
		elif method in {"restart_container", "stop_container"}:
			assert request.call_args.kwargs["query"] == {"t": 10}
		elif method == "remove_container":
			assert request.call_args.kwargs["query"] == {"v": 0, "force": 0}
		elif method == "pull_image":
			assert request.call_args.kwargs["query"] == {"fromImage": "repo:tag"}
		elif method == "container_logs":
			assert request.call_args.kwargs["raw_response"] is True

	def test_ssh_request_writes_http_request_and_reads_response(self) -> None:
		class RecordingBytesIO(io.BytesIO):
			def close(self):
				if not self.closed:
					self.closed_value = self.getvalue()
				super().close()

		api = DockerAPI.__new__(DockerAPI)
		api.endpoint = "ssh://user@docker-host:2222"
		stdin, stdout, stderr = RecordingBytesIO(), io.BytesIO(), io.BytesIO(b"")
		process = Mock(stdin=stdin, stdout=stdout, stderr=stderr)
		process.wait.return_value = 0
		process.poll.return_value = 0
		response = Mock(status=200)
		response.read.return_value = b"{}"
		with patch("docker_api.subprocess.Popen", return_value=process) as popen, patch(
			"docker_api.http.client.HTTPResponse", return_value=response
		):
			status, body = api._ssh_request("POST", "/v1.45/containers/create", b"{}")
		assert (status, body) == (200, b"{}")
		assert b"POST /v1.45/containers/create HTTP/1.1" in stdin.closed_value
		assert b"Content-Type: application/json" in stdin.closed_value
		assert popen.call_args.args[0][popen.call_args.args[0].index("-p") + 1] == "2222"

	def test_ssh_request_surfaces_remote_failure(self) -> None:
		api = DockerAPI.__new__(DockerAPI)
		api.endpoint = "ssh://docker-host"
		process = Mock(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(b"ssh denied"))
		process.wait.return_value = 1
		process.poll.return_value = 1
		response = Mock(status=200)
		response.read.return_value = b"{}"
		with patch("docker_api.subprocess.Popen", return_value=process), patch(
			"docker_api.http.client.HTTPResponse", return_value=response
		):
			with pytest.raises(DockerError, match="ssh denied"):
				api._ssh_request("GET", "/version", None)

	def test_request_builds_versioned_path_query_and_json_body(self) -> None:
		response = Mock(status=200)
		response.read.return_value = b'{"Id":"new"}'
		connection = Mock()
		connection.getresponse.return_value = response
		api = DockerAPI.__new__(DockerAPI)
		api.api_version = "1.45"
		api.socket_path = "/tmp/docker.sock"
		with patch("docker_api.UnixHTTPConnection", return_value=connection):
			result = api.request("POST", "/containers/create", query={"name": "a b"}, body={"Image": "x"})
		assert result == {"Id": "new"}
		args, kwargs = connection.request.call_args
		assert args == ("POST", "/v1.45/containers/create?name=a+b")
		assert json.loads(kwargs["body"]) == {"Image": "x"}
		connection.close.assert_called_once()

	def test_request_decodes_engine_errors(self) -> None:
		response = Mock(status=404)
		response.read.return_value = b'{"message":"not found"}'
		connection = Mock()
		connection.getresponse.return_value = response
		api = DockerAPI.__new__(DockerAPI)
		api.api_version, api.socket_path = "1.45", "/tmp/docker.sock"
		with patch("docker_api.UnixHTTPConnection", return_value=connection):
			with pytest.raises(DockerError, match="404: not found"):
				api.request("GET", "/containers/nope/json")

	def test_image_and_volume_paths_quote_names(self) -> None:
		api = DockerAPI.__new__(DockerAPI)
		api.api_version, api.socket_path = "1.45", "/tmp/docker.sock"
		with patch.object(api, "request") as request:
			api.remove_image("sha256:a/b")
			api.remove_volume("volume/name")
		assert request.call_args_list[0].args == ("DELETE", "/images/sha256%3Aa%2Fb")
		assert request.call_args_list[1].args == ("DELETE", "/volumes/volume%2Fname")

	def test_list_volumes_and_remove_volume_use_engine_endpoints(self) -> None:
		api = DockerAPI.__new__(DockerAPI)
		api.api_version, api.socket_path = "1.45", "/tmp/docker.sock"
		with patch.object(api, "request", return_value={"Volumes": [{"Name": "data"}]}) as request:
			assert api.list_volumes() == [{"Name": "data"}]
			api.remove_volume("data")
		assert request.call_args_list[0].args == ("GET", "/volumes")
		assert request.call_args_list[1].args == ("DELETE", "/volumes/data")

