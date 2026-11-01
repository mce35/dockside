from __future__ import annotations

import http.client
import json
import socket
import subprocess
from typing import Any
from urllib.parse import quote, urlencode, urlsplit


class DockerError(Exception):
	"""An error returned by the Docker Engine API."""


class UnixHTTPConnection(http.client.HTTPConnection):
	def __init__(self, socket_path: str) -> None:
		super().__init__("localhost", timeout=300)
		self.socket_path = socket_path

	def connect(self) -> None:
		self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.sock.settimeout(self.timeout)
		self.sock.connect(self.socket_path)


class _StreamResponseSocket:
	def __init__(self, stream: Any) -> None:
		self.stream = stream

	def makefile(self, mode: str) -> Any:
		return self.stream

	def close(self) -> None:
		pass


class DockerAPI:
	def __init__(self, endpoint: str) -> None:
		if "://" not in endpoint:
			endpoint = f"unix://{endpoint}"
		parsed_endpoint = urlsplit(endpoint)
		if parsed_endpoint.scheme == "unix":
			self.socket_path = parsed_endpoint.path
		elif parsed_endpoint.scheme == "ssh":
			self.socket_path = None
		elif parsed_endpoint.scheme == "tcp":
			raise DockerError(
				"TCP Docker contexts are not supported; use a Unix socket or SSH context."
			)
		else:
			raise DockerError(f"Unsupported Docker endpoint: {endpoint}")
		self.endpoint = endpoint
		version = self.request("GET", "/version", versioned=False)
		self.api_version = version["ApiVersion"]
		self.daemon_version = version.get("Version", "unknown")

	def _ssh_request(
		self, method: str, request_path: str, encoded_body: bytes | None
	) -> tuple[int, bytes]:
		parsed_endpoint = urlsplit(self.endpoint)
		host = parsed_endpoint.hostname
		if not host:
			raise DockerError(f"Invalid SSH Docker endpoint: {self.endpoint}")
		if ":" in host:
			host = f"[{host}]"
		destination = f"{parsed_endpoint.username}@{host}" if parsed_endpoint.username else host
		command = [
			"ssh",
			"-T",
			"-o",
			"BatchMode=yes",
			"-o",
			"StrictHostKeyChecking=yes",
		]
		if parsed_endpoint.port:
			command.extend(["-p", str(parsed_endpoint.port)])
		command.extend([destination, "docker", "system", "dial-stdio"])
		headers = [
			"Host: docker",
			"Connection: close",
			f"Content-Length: {len(encoded_body) if encoded_body is not None else 0}",
		]
		if encoded_body is not None:
			headers.append("Content-Type: application/json")
		request = (
			f"{method} {request_path} HTTP/1.1\r\n"
			+ "\r\n".join(headers)
			+ "\r\n\r\n"
		).encode()
		if encoded_body is not None:
			request += encoded_body
		process: subprocess.Popen[bytes] | None = None
		try:
			process = subprocess.Popen(
				command,
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.PIPE,
			)
			if process.stdin is None or process.stdout is None or process.stderr is None:
				raise DockerError("Could not open SSH Docker request streams.")
			process.stdin.write(request)
			process.stdin.flush()
			response = http.client.HTTPResponse(
				_StreamResponseSocket(process.stdout)
			)
			response.begin()
			status = response.status
			response_body = response.read()
			process.stdin.close()
			return_code = process.wait(timeout=300)
			stderr = process.stderr.read().decode(errors="replace").strip()
			if return_code:
				raise DockerError(stderr or "SSH Docker request failed.")
			return status, response_body
		except (http.client.HTTPException, OSError, subprocess.TimeoutExpired) as error:
			raise DockerError(f"SSH Docker request failed: {error}") from error
		finally:
			if process is not None:
				if process.poll() is None:
					process.kill()
					process.wait()
				for stream in (process.stdin, process.stdout, process.stderr):
					if stream is not None:
						stream.close()

	def request(
		self,
		method: str,
		path: str,
		*,
		query: dict[str, Any] | None = None,
		body: dict[str, Any] | None = None,
		versioned: bool = True,
		raw_response: bool = False,
	) -> Any:
		request_path = f"/v{self.api_version}{path}" if versioned else path
		if query:
			request_path += "?" + urlencode(query, doseq=True)
		headers = {"Content-Type": "application/json"} if body is not None else {}
		encoded_body = json.dumps(body).encode() if body is not None else None

		if self.socket_path is None:
			status, response_body = self._ssh_request(
				method, request_path, encoded_body
			)
		else:
			connection = UnixHTTPConnection(self.socket_path)
			try:
				connection.request(method, request_path, body=encoded_body, headers=headers)
				response = connection.getresponse()
				status = response.status
				response_body = response.read()
			finally:
				connection.close()

		if status >= 400:
			try:
				detail = json.loads(response_body).get("message", response_body.decode())
			except (json.JSONDecodeError, AttributeError):
				detail = response_body.decode(errors="replace")
			raise DockerError(f"Docker API {status}: {detail}")
		if raw_response:
			return response_body
		if not response_body:
			return None
		try:
			return json.loads(response_body)
		except json.JSONDecodeError:
			return response_body.decode(errors="replace")

	def list_containers(self) -> list[dict[str, Any]]:
		containers = self.request("GET", "/containers/json", query={"all": 1})
		for container in containers:
			image = container.get("Image", "")
			if not image.startswith("sha256:"):
				continue

			inspection = self.inspect_container(container["Id"])
			configured_image = (inspection.get("Config") or {}).get("Image", "")
			if configured_image and not configured_image.startswith("sha256:"):
				container["Image"] = configured_image
				continue

			image_id = container.get("ImageID") or image
			image_inspection = self.inspect_image(image_id)
			repository_tags = image_inspection.get("RepoTags") or []
			if repository_tags and repository_tags[0] != "<none>:<none>":
				container["Image"] = repository_tags[0]
		return containers

	def inspect_container(self, container_id: str) -> dict[str, Any]:
		return self.request("GET", f"/containers/{quote(container_id, safe='')}/json")

	def container_stats(self, container_id: str) -> dict[str, Any]:
		return self.request(
			"GET",
			f"/containers/{quote(container_id, safe='')}/stats",
			query={"stream": 0, "one-shot": 1},
		)

	def inspect_image(self, image: str) -> dict[str, Any]:
		return self.request("GET", f"/images/{quote(image, safe='')}/json")

	def container_logs(self, container_id: str) -> bytes:
		return self.request(
			"GET",
			f"/containers/{quote(container_id, safe='')}/logs",
			query={"stdout": 1, "stderr": 1, "timestamps": 1, "follow": 0, "tail": 1000},
			raw_response=True,
		)

	def start_container(self, container_id: str) -> None:
		self.request("POST", f"/containers/{quote(container_id, safe='')}/start")

	def restart_container(self, container_id: str) -> None:
		self.request(
			"POST",
			f"/containers/{quote(container_id, safe='')}/restart",
			query={"t": 10},
		)

	def stop_container(self, container_id: str) -> None:
		self.request(
			"POST",
			f"/containers/{quote(container_id, safe='')}/stop",
			query={"t": 10},
		)

	def remove_container(self, container_id: str) -> None:
		self.request(
			"DELETE",
			f"/containers/{quote(container_id, safe='')}",
			query={"v": 0, "force": 0},
		)

	def pull_image(self, image: str) -> None:
		self.request("POST", "/images/create", query={"fromImage": image})

	def create_container(
		self, name: str, payload: dict[str, Any]
	) -> dict[str, Any]:
		return self.request(
			"POST", "/containers/create", query={"name": name}, body=payload
		)
