# Dockside

Dockside is an interactive Linux terminal tool for managing Docker containers. It lists containers, checks whether their image matches the remote registry, and supports container actions, logs, and background status updates.

## Requirements

- Linux with Python 3.10 or later
- Docker Engine and access to its Unix socket (usually `/var/run/docker.sock`)
- `less` for viewing container logs
- A terminal that supports ANSI colors and keyboard input

No third-party Python packages are required.

## Run

From the repository directory:

```sh
python3 dockside.py
```

To use a non-default Docker socket:

```sh
python3 dockside.py --socket /path/to/docker.sock
```

The `DOCKER_HOST=unix:///path/to/docker.sock` environment variable is also supported. Your user must have permission to access the Docker socket.

## Picker Controls

- Up/Down: move the active row
- Left/Right: change the sort column; `s` toggles ascending/descending
- Space: toggle the active container's selection
- Ctrl+A: select all containers (`A` also works)
- `c`: clear the selection
- Enter: open the action menu for selected containers
- `l`: view logs for the active or selected containers
- `i`: inspect the active or selected containers
- Ctrl+S: stop
- `r`: restart a running container or start a stopped container
- Ctrl+R: recreate
- `p`: pull the image without changing the container
- Ctrl+D: delete after confirmation
- `a` / `q`: scroll the in-memory action log older / newer
- Ctrl+Q: quit

Actions run in the background where possible. Timestamped progress, results, and errors appear below the container list. The list refreshes periodically and after actions complete, while preserving the active row and surviving selections.

## Image Status

The image column compares each container's image configuration with the matching platform manifest in its remote registry. Docker credentials are read from the configured Docker client credential store or `config.json`. Registry access or authentication failures display as `UNKNOWN`. The check does not pull images.

## Logs

Logs open in `less` at the newest entries. Multi-container log views merge timestamped lines chronologically, use a colored container-name prefix, and request at most the latest 1,000 lines per container.

## Modules

- `dockside.py`: interactive picker and application entry point
- `docker_api.py`: Docker Engine API client over a Unix socket
- `container_models.py`: picker state, recreate plan, and in-memory action log
- `container_ops.py`: container configuration preservation and background actions
- `image_registry.py`: remote registry manifest checks and freshness status
- `container_logs.py`: log retrieval, merging, and pager integration
- `terminal_colors.py`: ANSI color constants
