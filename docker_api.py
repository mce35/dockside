from __future__ import annotations

import http.client
import json
import socket
from typing import Any
from urllib.parse import quote, urlencode


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


class DockerAPI:
	def __init__(self, socket_path: str) -> None:
		self.socket_path = socket_path
		version = self.request("GET", "/version", versioned=False)
		self.api_version = version["ApiVersion"]

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

		connection = UnixHTTPConnection(self.socket_path)
		try:
			connection.request(method, request_path, body=encoded_body, headers=headers)
			response = connection.getresponse()
			response_body = response.read()
		finally:
			connection.close()

		if response.status >= 400:
			try:
				detail = json.loads(response_body).get("message", response_body.decode())
			except (json.JSONDecodeError, AttributeError):
				detail = response_body.decode(errors="replace")
			raise DockerError(f"Docker API {response.status}: {detail}")
		if raw_response:
			return response_body
		if not response_body:
			return None
		try:
			return json.loads(response_body)
		except json.JSONDecodeError:
			return response_body.decode(errors="replace")

	def list_containers(self) -> list[dict[str, Any]]:
		return self.request("GET", "/containers/json", query={"all": 1})

	def inspect_container(self, container_id: str) -> dict[str, Any]:
		return self.request("GET", f"/containers/{quote(container_id, safe='')}/json")

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
