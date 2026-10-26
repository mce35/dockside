from __future__ import annotations

import base64
import http.client
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlencode

from docker_api import DockerAPI, DockerError


def _split_image_reference(image: str) -> tuple[str, str, str]:
	name, separator, digest = image.partition("@")
	parts = name.split("/")
	if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
		registry = parts.pop(0)
	else:
		registry = "docker.io"
	if registry == "docker.io":
		registry = "registry-1.docker.io"
		if len(parts) == 1:
			parts.insert(0, "library")
	repository = "/".join(parts)
	if separator:
		reference = digest
	else:
		last_component = parts[-1]
		if ":" in last_component:
			repository = "/".join(parts[:-1] + [last_component.rsplit(":", 1)[0]])
			reference = last_component.rsplit(":", 1)[1]
		else:
			reference = "latest"
	return registry, repository, reference


def _registry_credentials(registry: str) -> tuple[str, str] | str | None:
	config_path = os.path.join(
		os.environ.get("DOCKER_CONFIG", os.path.expanduser("~/.docker")),
		"config.json",
	)
	try:
		with open(config_path, encoding="utf-8") as config_file:
			config = json.load(config_file)
	except (OSError, json.JSONDecodeError):
		return None

	registry_keys = [registry, f"https://{registry}", f"https://{registry}/v1/"]
	if registry == "registry-1.docker.io":
		registry_keys.extend(
			["docker.io", "https://index.docker.io/v1/", "https://index.docker.io"]
		)
	credential_helpers = config.get("credHelpers", {})
	helper_name = next(
		(credential_helpers[key] for key in registry_keys if key in credential_helpers),
		config.get("credsStore"),
	)
	if helper_name:
		server_address = (
			"https://index.docker.io/v1/"
			if registry == "registry-1.docker.io"
			else f"https://{registry}"
		)
		try:
			result = subprocess.run(
				[f"docker-credential-{helper_name}", "get"],
				input=server_address,
				capture_output=True,
				text=True,
				check=True,
				timeout=10,
			)
			credentials = json.loads(result.stdout)
			return credentials["Username"], credentials["Secret"]
		except (OSError, subprocess.SubprocessError, json.JSONDecodeError, KeyError):
			pass

	auth_entries = config.get("auths", {})
	for registry_key in registry_keys:
		entry = auth_entries.get(registry_key)
		if not entry:
			continue
		if entry.get("identitytoken"):
			return entry["identitytoken"]
		if entry.get("username") is not None and entry.get("password") is not None:
			return entry["username"], entry["password"]
		if entry.get("auth"):
			try:
				decoded = base64.b64decode(entry["auth"]).decode()
				username, password = decoded.split(":", 1)
				return username, password
			except (ValueError, UnicodeDecodeError):
				continue
	return None


def _registry_manifest(
	url: str,
	credentials: tuple[str, str] | str | None,
	accept: str,
) -> dict[str, Any]:
	headers = {"Accept": accept}
	try:
		request = urllib.request.Request(url, headers=headers)
		with urllib.request.urlopen(request, timeout=15) as response:
			return json.loads(response.read())
	except urllib.error.HTTPError as error:
		if error.code != 401:
			raise DockerError(f"Registry returned HTTP {error.code}.") from error
		challenge = error.headers.get("WWW-Authenticate", "")
	match = re.match(r"(Bearer|Basic)\s+(.*)", challenge, re.IGNORECASE)
	if not match:
		if isinstance(credentials, tuple):
			headers["Authorization"] = "Basic " + base64.b64encode(
				f"{credentials[0]}:{credentials[1]}".encode()
			).decode()
		else:
			raise DockerError("Registry authentication is required.")
	else:
		scheme, challenge_parameters = match.groups()
		parameters = dict(re.findall(r'([\w-]+)="([^"]*)"', challenge_parameters))
		if scheme.casefold() == "basic":
			if not isinstance(credentials, tuple):
				raise DockerError("Registry credentials are unavailable.")
			headers["Authorization"] = "Basic " + base64.b64encode(
				f"{credentials[0]}:{credentials[1]}".encode()
			).decode()
		else:
			realm = parameters.get("realm")
			if not realm:
				raise DockerError("Registry bearer challenge has no token realm.")
			token_query = {
				key: value
				for key, value in {
					"service": parameters.get("service"),
					"scope": parameters.get("scope"),
				}.items()
				if value
			}
			token_url = realm + ("&" if "?" in realm else "?") + urlencode(token_query)
			token_headers: dict[str, str] = {}
			if isinstance(credentials, tuple):
				token_headers["Authorization"] = "Basic " + base64.b64encode(
					f"{credentials[0]}:{credentials[1]}".encode()
				).decode()
			try:
				token_request = urllib.request.Request(token_url, headers=token_headers)
				with urllib.request.urlopen(token_request, timeout=15) as response:
					token_response = json.loads(response.read())
			except (urllib.error.URLError, json.JSONDecodeError) as token_error:
				raise DockerError("Could not authenticate with the registry.") from token_error
			token = token_response.get("token") or token_response.get("access_token")
			if not token:
				raise DockerError("Registry token response contained no token.")
			headers["Authorization"] = f"Bearer {token}"

	try:
		request = urllib.request.Request(url, headers=headers)
		with urllib.request.urlopen(request, timeout=15) as response:
			return json.loads(response.read())
	except (urllib.error.URLError, json.JSONDecodeError) as registry_error:
		raise DockerError("Could not read the image manifest from the registry.") from registry_error


def _remote_image_config_digest(
	image: str, platform: dict[str, str]
) -> str:
	registry, repository, reference = _split_image_reference(image)
	credentials = _registry_credentials(registry)
	manifest_types = ", ".join(
		[
			"application/vnd.oci.image.index.v1+json",
			"application/vnd.docker.distribution.manifest.list.v2+json",
			"application/vnd.oci.image.manifest.v1+json",
			"application/vnd.docker.distribution.manifest.v2+json",
		]
	)
	manifest_url = (
		f"https://{registry}/v2/{quote(repository, safe='/')}/manifests/"
		f"{quote(reference, safe=':') }"
	)
	manifest = _registry_manifest(manifest_url, credentials, manifest_types)
	if manifest.get("manifests"):
		platforms = manifest["manifests"]
		matching_platforms = [
			descriptor
			for descriptor in platforms
			if descriptor.get("platform", {}).get("os") == platform.get("Os")
			and descriptor.get("platform", {}).get("architecture")
			== platform.get("Architecture")
			and (
				not platform.get("Variant")
				or not descriptor.get("platform", {}).get("variant")
				or descriptor.get("platform", {}).get("variant") == platform["Variant"]
			)
		]
		if not matching_platforms:
			raise DockerError("No matching platform manifest exists in the registry.")
		manifest_digest = matching_platforms[0].get("digest")
		if not manifest_digest:
			raise DockerError("Platform manifest has no digest.")
		manifest_url = (
			f"https://{registry}/v2/{quote(repository, safe='/')}/manifests/"
			f"{quote(manifest_digest, safe=':')}"
		)
		manifest = _registry_manifest(manifest_url, credentials, manifest_types)
	config_digest = manifest.get("config", {}).get("digest")
	if not config_digest:
		raise DockerError("Registry manifest has no image configuration digest.")
	return config_digest


def _safe_image_freshness(
	api: DockerAPI, image: str, image_id: str
) -> str:
	try:
		local_image = api.inspect_image(image_id)
		platform = {
			"Os": local_image.get("Os", ""),
			"Architecture": local_image.get("Architecture", ""),
			"Variant": local_image.get("Variant", ""),
		}
		remote_image_id = _remote_image_config_digest(image, platform)
		return "CURRENT" if image_id == remote_image_id else "OUTDATED"
	except (DockerError, OSError, ValueError, KeyError, http.client.HTTPException):
		return "UNKNOWN"


class RegistryChecks:
	def __init__(
		self,
		api: DockerAPI,
		executor: ThreadPoolExecutor,
		cache: dict[tuple[str, str], str],
	) -> None:
		self.api = api
		self.executor = executor
		self.cache = cache
		self.futures: dict[tuple[str, str], Future[str]] = {}
		self.container_keys: dict[str, tuple[str, str] | None] = {}

	def update(self, containers: list[dict[str, Any]]) -> None:
		active_container_ids = {container["Id"] for container in containers}
		for container_id in self.container_keys.keys() - active_container_ids:
			self.container_keys.pop(container_id, None)
		for container in containers:
			image = container.get("Image")
			image_id = container.get("ImageID")
			if not image or not image_id:
				self.container_keys[container["Id"]] = None
				continue
			cache_key = (image, image_id)
			self.container_keys[container["Id"]] = cache_key
			if cache_key not in self.cache and cache_key not in self.futures:
				self.futures[cache_key] = self.executor.submit(
					_safe_image_freshness, self.api, image, image_id
				)

	def snapshot(
		self, containers: list[dict[str, Any]]
	) -> tuple[dict[str, str], str]:
		for cache_key, future in list(self.futures.items()):
			if not future.done():
				continue
			try:
				self.cache[cache_key] = future.result()
			except Exception:
				self.cache[cache_key] = "UNKNOWN"
			del self.futures[cache_key]

		freshness: dict[str, str] = {}
		active_keys: set[tuple[str, str]] = set()
		for container in containers:
			container_id = container["Id"]
			cache_key = self.container_keys.get(container_id)
			if cache_key is None:
				freshness[container_id] = "UNKNOWN"
				continue
			active_keys.add(cache_key)
			if cache_key in self.futures:
				freshness[container_id] = "CHECKING"
				continue
			freshness[container_id] = self.cache.get(cache_key, "UNKNOWN")
		completed = sum(cache_key in self.cache for cache_key in active_keys)
		pending = len(active_keys) - completed
		progress = (
			f"Registry checks: {completed}/{len(active_keys)} complete"
			if not pending
			else f"Registry checks: {completed}/{len(active_keys)} complete; {pending} in progress"
		)
		return freshness, progress

	def invalidate(self, containers: list[dict[str, Any]]) -> None:
		for container in containers:
			cache_key = self.container_keys.get(container["Id"])
			if cache_key is None:
				continue
			self.cache.pop(cache_key, None)
			future = self.futures.pop(cache_key, None)
			if future is not None:
				future.cancel()
