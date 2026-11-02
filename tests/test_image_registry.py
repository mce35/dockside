import base64
import email.message
import io
import json
import os
import tempfile
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import image_registry
from docker_api import DockerError

class TestRegistryTests:
	def test_split_image_references(self) -> None:
		assert image_registry._split_image_reference("ubuntu") == ("registry-1.docker.io", "library/ubuntu", "latest")
		assert image_registry._split_image_reference("team/app:2") == ("registry-1.docker.io", "team/app", "2")
		assert image_registry._split_image_reference("ghcr.io/team/app@sha256:abc") == ("ghcr.io", "team/app", "sha256:abc")
		assert image_registry._split_image_reference("localhost:5000/app") == ("localhost:5000", "app", "latest")

	def test_registry_credentials_reads_auth_and_identity_token(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			config_path = Path(directory) / "config.json"
			config_path.write_text(json.dumps({"auths": {
				"ghcr.io": {"auth": base64.b64encode(b"alice:secret").decode()},
				"other.io": {"identitytoken": "token-value"},
			}}))
			with patch.dict("os.environ", {"DOCKER_CONFIG": directory}):
				assert image_registry._registry_credentials("ghcr.io") == ("alice", "secret")
				assert image_registry._registry_credentials("other.io") == "token-value"

	def test_registry_credentials_uses_helper_then_falls_back_to_auth_file(self, monkeypatch) -> None:
		with tempfile.TemporaryDirectory() as directory:
			Path(directory, "config.json").write_text(json.dumps({
				"credHelpers": {"example.io": "test"},
				"auths": {"example.io": {"username": "file-user", "password": "file-pass"}},
			}))
			monkeypatch.setenv("DOCKER_CONFIG", directory)
			result = Mock(stdout=json.dumps({"Username": "helper-user", "Secret": "helper-secret"}))
			with patch("image_registry.subprocess.run", return_value=result) as run:
				assert image_registry._registry_credentials("example.io") == ("helper-user", "helper-secret")
			assert run.call_args.args[0] == ["docker-credential-test", "get"]
			with patch("image_registry.subprocess.run", side_effect=OSError("missing helper")):
				assert image_registry._registry_credentials("example.io") == ("file-user", "file-pass")

	def test_registry_credentials_supports_default_store_and_docker_hub_aliases(self, monkeypatch) -> None:
		with tempfile.TemporaryDirectory() as directory:
			Path(directory, "config.json").write_text(json.dumps({"credsStore": "secretservice"}))
			monkeypatch.setenv("DOCKER_CONFIG", directory)
			result = Mock(stdout=json.dumps({"Username": "hub-user", "Secret": "hub-secret"}))
			with patch("image_registry.subprocess.run", return_value=result) as run:
				assert image_registry._registry_credentials("registry-1.docker.io") == ("hub-user", "hub-secret")
			assert run.call_args.kwargs["input"] == "https://index.docker.io/v1/"

	def test_registry_credentials_handles_bad_config_helper_and_auth(self, monkeypatch) -> None:
		with tempfile.TemporaryDirectory() as directory:
			config = Path(directory, "config.json")
			monkeypatch.setenv("DOCKER_CONFIG", directory)
			config.write_text("{")
			assert image_registry._registry_credentials("example.io") is None
			config.write_text(json.dumps({"auths": {"example.io": {"auth": "%%%"}}}))
			assert image_registry._registry_credentials("example.io") is None

	@staticmethod
	def _http_error(challenge: str = "", code: int = 401) -> urllib.error.HTTPError:
		headers = email.message.Message()
		if challenge:
			headers["WWW-Authenticate"] = challenge
		return urllib.error.HTTPError("https://example.io/manifest", code, "error", headers, io.BytesIO(b"{}"))

	@staticmethod
	def _response(payload: bytes) -> Mock:
		response = Mock()
		response.read.return_value = payload
		response.__enter__ = Mock(return_value=response)
		response.__exit__ = Mock(return_value=False)
		return response

	def test_registry_manifest_returns_public_manifest_and_propagates_non_auth_http_error(self) -> None:
		response = self._response(b'{"config":{"digest":"sha256:ok"}}')
		with patch("image_registry.urllib.request.urlopen", return_value=response):
			assert image_registry._registry_manifest("https://example.io/m", None, "accept") == {"config": {"digest": "sha256:ok"}}
		with patch("image_registry.urllib.request.urlopen", side_effect=self._http_error(code=503)):
			with pytest.raises(DockerError, match="HTTP 503"):
				image_registry._registry_manifest("https://example.io/m", None, "accept")

	def test_registry_manifest_retries_basic_challenge_with_basic_header(self) -> None:
		manifest = self._response(b'{"ok":true}')
		with patch("image_registry.urllib.request.urlopen", side_effect=[self._http_error('Basic realm="example"'), manifest]) as urlopen:
			assert image_registry._registry_manifest("https://example.io/m", ("alice", "secret"), "accept") == {"ok": True}
		request = urlopen.call_args_list[1].args[0]
		assert request.get_header("Authorization") == "Basic " + base64.b64encode(b"alice:secret").decode()

	def test_registry_manifest_bearer_challenge_gets_token_then_manifest(self) -> None:
		token_response = self._response(b'{"token":"bearer-token"}')
		manifest = self._response(b'{"ok":true}')
		challenge = 'Bearer realm="https://auth.example/token?x=1",service="registry",scope="repo:pull"'
		with patch("image_registry.urllib.request.urlopen", side_effect=[self._http_error(challenge), token_response, manifest]) as urlopen:
			assert image_registry._registry_manifest("https://example.io/m", ("alice", "secret"), "accept") == {"ok": True}
		token_request = urlopen.call_args_list[1].args[0]
		assert "service=registry" in token_request.full_url
		assert token_request.get_header("Authorization").startswith("Basic ")
		manifest_request = urlopen.call_args_list[2].args[0]
		assert manifest_request.get_header("Authorization") == "Bearer bearer-token"

	@pytest.mark.parametrize(
		("challenge", "credentials", "message"),
		[
			("", None, "authentication is required"),
			('Basic realm="x"', None, "credentials are unavailable"),
			('Bearer service="registry"', None, "no token realm"),
		],
	)
	def test_registry_manifest_rejects_incomplete_auth(self, challenge, credentials, message) -> None:
		with patch("image_registry.urllib.request.urlopen", side_effect=self._http_error(challenge)):
			with pytest.raises(DockerError, match=message):
				image_registry._registry_manifest("https://example.io/m", credentials, "accept")

	def test_registry_manifest_reports_bad_token_and_registry_responses(self) -> None:
		challenge = 'Bearer realm="https://auth.example/token"'
		with patch("image_registry.urllib.request.urlopen", side_effect=[self._http_error(challenge), self._response(b'{}')]):
			with pytest.raises(DockerError, match="contained no token"):
				image_registry._registry_manifest("https://example.io/m", None, "accept")
		with patch("image_registry.urllib.request.urlopen", side_effect=[self._http_error(challenge), urllib.error.URLError("offline")]):
			with pytest.raises(DockerError, match="Could not authenticate"):
				image_registry._registry_manifest("https://example.io/m", None, "accept")
		with patch("image_registry.urllib.request.urlopen", side_effect=[
			self._http_error(challenge), self._response(b'{"token":"t"}'), urllib.error.URLError("offline")
		]):
			with pytest.raises(DockerError, match="Could not read"):
				image_registry._registry_manifest("https://example.io/m", None, "accept")

	def test_safe_freshness_returns_unknown_when_registry_fails(self) -> None:
		api = Mock()
		api.inspect_image.side_effect = DockerError("offline")
		assert image_registry._safe_image_freshness(api, "repo:tag", "sha256:abc") == "UNKNOWN"

	def test_remote_digest_selects_matching_platform_manifest(self) -> None:
		index_manifest = {"manifests": [
			{"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
			{"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
		]}
		platform_manifest = {"config": {"digest": "sha256:config"}}
		with patch.object(image_registry, "_registry_credentials", return_value=None), patch.object(
			image_registry, "_registry_manifest", side_effect=[index_manifest, platform_manifest]
		) as get_manifest:
			digest = image_registry._remote_image_config_digest(
				"repo/app:tag", {"Os": "linux", "Architecture": "amd64", "Variant": ""}
			)
		assert digest == "sha256:config"
		assert "sha256:amd" in get_manifest.call_args_list[1].args[0]

	def test_registry_checks_reuses_cache_and_marks_missing_image_unknown(self) -> None:
		with ThreadPoolExecutor(max_workers=1) as executor:
			api = Mock()
			cache = {("repo:tag", "sha256:a"): "CURRENT"}
			checks = image_registry.RegistryChecks(api, executor, cache)
			containers = [
				{"Id": "one", "Image": "repo:tag", "ImageID": "sha256:a"},
				{"Id": "two", "Image": "", "ImageID": ""},
			]
			checks.update(containers)
			freshness, progress = checks.snapshot(containers)
		assert freshness == {"one": "CURRENT", "two": "UNKNOWN"}
		assert "1/1 complete" in progress

