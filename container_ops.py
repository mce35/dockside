from __future__ import annotations

import copy
from typing import Any

from container_models import ActionLog, RecreatePlan
from docker_api import DockerAPI, DockerError


def _mount_destination(bind: str) -> str:
	parts = bind.split(":")
	return parts[1] if len(parts) > 1 else ""


def _preserve_mount_sources(
	inspection: dict[str, Any], host_config: dict[str, Any]
) -> None:
	"""Reuse existing anonymous volume sources instead of allocating empty ones."""
	observed_mounts = inspection.get("Mounts", [])
	configured_mounts = host_config.get("Mounts") or []
	mounts_by_target = {
		mount.get("Target") or mount.get("Destination"): mount
		for mount in observed_mounts
		if mount.get("Target") or mount.get("Destination")
	}

	configured_targets: set[str] = set()
	for configured_mount in configured_mounts:
		target = configured_mount.get("Target")
		if not target:
			continue
		configured_targets.add(target)
		actual_mount = mounts_by_target.get(target)
		if actual_mount and actual_mount.get("Source"):
			configured_mount["Source"] = actual_mount["Source"]

	bind_targets = {
		_mount_destination(bind)
		for bind in host_config.get("Binds") or []
		if _mount_destination(bind)
	}
	for actual_mount in observed_mounts:
		target = actual_mount.get("Target") or actual_mount.get("Destination")
		mount_type = actual_mount.get("Type")
		if (
			not target
			or target in configured_targets
			or target in bind_targets
			or mount_type not in {"bind", "volume"}
		):
			continue

		preserved_mount: dict[str, Any] = {
			"Type": mount_type,
			"Target": target,
			"ReadOnly": not actual_mount.get("RW", True),
		}
		source = actual_mount.get("Source")
		if source:
			preserved_mount["Source"] = source
		if mount_type == "bind" and actual_mount.get("Propagation"):
			preserved_mount["BindOptions"] = {
				"Propagation": actual_mount["Propagation"]
			}
		configured_mounts.append(preserved_mount)
		configured_targets.add(target)

	if configured_mounts:
		host_config["Mounts"] = configured_mounts


def _create_payload(
	inspection: dict[str, Any], image: str
) -> dict[str, Any]:
	config = copy.deepcopy(inspection.get("Config") or {})
	host_config = copy.deepcopy(inspection.get("HostConfig") or {})
	config["Image"] = image
	_preserve_mount_sources(inspection, host_config)

	payload: dict[str, Any] = {**config, "HostConfig": host_config}
	networks = inspection.get("NetworkSettings", {}).get("Networks", {})
	endpoint_configs: dict[str, Any] = {}
	endpoint_fields = {
		"IPAMConfig",
		"Links",
		"Aliases",
		"MacAddress",
		"DriverOpts",
		"GwPriority",
	}
	for network_name, settings in networks.items():
		endpoint_configs[network_name] = {
			key: copy.deepcopy(value)
			for key, value in settings.items()
			if key in endpoint_fields and value is not None
		}
	if endpoint_configs:
		payload["NetworkingConfig"] = {"EndpointsConfig": endpoint_configs}
	return payload


def _container_name(container: dict[str, Any]) -> str:
	names = container.get("Names") or []
	return names[0].lstrip("/") if names else container["Id"][:12]


def _container_display_name(container: dict[str, Any]) -> str:
	name = _container_name(container)
	context_name = container.get("_DocksideContext")
	return f"{context_name}/{name}" if context_name else name


def _prepare_recreate(
	api: DockerAPI,
	container_id: str,
	context_name: str | None = None,
) -> RecreatePlan:
	inspection = api.inspect_container(container_id)
	name = inspection.get("Name", "").lstrip("/") or container_id[:12]
	config = inspection.get("Config") or {}
	image = config.get("Image") or inspection.get("Image")
	if not image:
		raise DockerError("Could not determine the container's image.")
	was_running = bool(inspection.get("State", {}).get("Running"))
	auto_remove = bool((inspection.get("HostConfig") or {}).get("AutoRemove"))
	return RecreatePlan(
		container_id=container_id,
		name=name,
		image=image,
		inspection=inspection,
		payload=_create_payload(inspection, image),
		was_running=was_running,
		auto_remove=auto_remove,
		pull_first=False,
		context_name=context_name,
	)


def _plan_display_name(plan: RecreatePlan) -> str:
	return f"{plan.context_name}/{plan.name}" if plan.context_name else plan.name


def _execute_recreate(
	api: DockerAPI, plan: RecreatePlan, action_log: ActionLog
) -> None:
	display_name = _plan_display_name(plan)
	if plan.pull_first:
		action_log.append(f"Pulling {plan.image} before recreating {display_name}.")
		api.pull_image(plan.image)
	if plan.was_running:
		action_log.append(f"Stopping container {display_name} before recreation.")
		api.stop_container(plan.container_id)
	if not (plan.was_running and plan.auto_remove):
		api.remove_container(plan.container_id)
	try:
		created = api.create_container(plan.name, plan.payload)
	except DockerError as create_error:
		action_log.append(f"ERROR recreating {display_name}: {create_error}")
		action_log.append(f"Attempting to restore the previous configuration for {display_name}.")
		try:
			restore_payload = copy.deepcopy(plan.payload)
			original_image_id = plan.inspection.get("Image")
			if original_image_id:
				restore_payload["Image"] = original_image_id
			restored = api.create_container(plan.name, restore_payload)
			if plan.was_running:
				api.start_container(restored["Id"])
			action_log.append(
				f"Previous configuration restored for {display_name}.", refresh=True
			)
			return
		except (DockerError, OSError) as restore_error:
			action_log.append(
				f"ERROR restoring {display_name}: {restore_error}", refresh=True
			)
			raise
	if plan.was_running:
		action_log.append(f"Starting recreated container {display_name}.")
		api.start_container(created["Id"])
	action_log.append(f"Container {display_name} recreated successfully.", refresh=True)


def _delete_container(api: DockerAPI, container_id: str) -> None:
	inspection = api.inspect_container(container_id)
	was_running = bool(inspection.get("State", {}).get("Running"))
	auto_remove = bool((inspection.get("HostConfig") or {}).get("AutoRemove"))
	if was_running:
		api.stop_container(container_id)
	if not (was_running and auto_remove):
		api.remove_container(container_id)


def _run_recreate_logged(
	api: DockerAPI, plan: RecreatePlan, action_log: ActionLog
) -> None:
	try:
		_execute_recreate(api, plan, action_log)
	except (DockerError, OSError) as error:
		action_log.append(f"ERROR recreating {_plan_display_name(plan)}: {error}", refresh=True)


def _run_background_action(
	api: DockerAPI,
	container: dict[str, Any],
	action: str,
	action_log: ActionLog,
) -> None:
	name = _container_display_name(container)
	container_id = container["Id"]
	try:
		if action == "start":
			if container.get("State") == "running":
				action_log.append(
					f"Skipped starting {name}; it is already running.", refresh=True
				)
				return
			api.start_container(container_id)
		elif action == "stop":
			if container.get("State") != "running":
				action_log.append(
					f"Skipped stopping {name}; it is not running.", refresh=True
				)
				return
			api.stop_container(container_id)
		elif action == "restart":
			if container.get("State") == "running":
				api.restart_container(container_id)
			else:
				api.start_container(container_id)
		elif action == "pull":
			inspection = api.inspect_container(container_id)
			image = (inspection.get("Config") or {}).get("Image")
			if not image:
				raise DockerError("Could not determine the container's configured image.")
			action_log.append(f"Pulling image {image} for container {name}.")
			api.pull_image(image)
		elif action == "delete":
			_delete_container(api, container_id)
		action_labels = {
			"start": "Started",
			"stop": "Stopped",
			"restart": "Restarted",
			"pull": "Image pull completed for",
			"delete": "Deleted",
		}
		action_log.append(f"{action_labels[action]} container {name}.", refresh=True)
	except (DockerError, OSError, KeyError) as error:
		action_log.append(f"ERROR: {name}: {error}", refresh=True)
