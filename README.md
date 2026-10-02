# tenki-harbor

Run [Harbor](https://github.com/harbor-framework/harbor) agent evaluations on
[Tenki Sandbox](https://tenki.cloud/docs/sandbox) VMs.

Harbor runs coding-agent harnesses (Codex, OpenCode, Claude Code, mini-swe-agent,
Terminus, …) against graded benchmarks such as Terminal-Bench 2.0. This package
adds Tenki as a place to run those trials: every trial gets its own Tenki VM.

## Quickstart

```bash
git clone <this repo> && cd tenki-harbor && uv sync   # not on PyPI yet
export TENKI_API_KEY=tk_...

# Sanity check with the reference solution (no model needed)
uv run harbor run -t hello-world/hello-world -e tenki_harbor:TenkiEnvironment --agent oracle
```

Run one model across several harnesses on Terminal-Bench 2.0:

```bash
export OPENAI_BASE_URL=https://your-inference-endpoint/v1
export OPENAI_API_KEY=...
# edit `your-model-id` in the config first
uv run harbor run -c examples/harness-matrix.yaml
uv run harbor view jobs/harness-matrix     # pass rates and full agent trajectories
```

Any harness can also be run on its own:

```bash
uv run harbor run -d terminal-bench@2.0 -e tenki_harbor:TenkiEnvironment \
  --agent codex --model openai/your-model-id --n-concurrent 16 \
  --ae OPENAI_BASE_URL=$OPENAI_BASE_URL --ae OPENAI_API_KEY=$OPENAI_API_KEY
```

## How it works

Tenki VMs boot Tenki images, not OCI images, so the task's container runs under
Docker inside the VM:

1. Create a Tenki session sized to the task (`cpus`, `memory_mb`, `storage_mb`).
2. Install and start Docker in the VM (about 12 s on the base image).
3. `docker pull` the task's `docker_image`, or `docker build` its Dockerfile.
4. Route Harbor's `exec` and file transfers into that container with
   `docker exec` / `docker cp`.
5. Terminate the session when the trial ends.

Sessions are tagged `harbor` and carry the Harbor session id in their metadata.

## Options

Pass with `--ek key=value` or under `environment.kwargs` in a job config.

| Option | Default | |
|---|---|---|
| `max_duration_sec` | `7200` | Hard VM lifetime, so a crashed run can't leak a VM. Capped by the workspace limit. |
| `disk_size_gb` | task storage + 10 GB, min 20 | VM disk, which also holds the image layers. |
| `image` / `snapshot_id` | Tenki base image | Start from a Tenki image or snapshot, e.g. one with Docker preinstalled. |
| `base_url` | `TENKI_API_ENDPOINT` or `https://api.tenki.cloud` | Tenki API endpoint. |

## Supported today

| Feature | Status |
|---|---|
| Prebuilt `docker_image` tasks (all of Terminal-Bench 2.0) | Yes |
| Dockerfile tasks (built in the VM) | Yes |
| `network_mode = "no-network"`, including switching between phases | Yes |
| `docker-compose.yaml` tasks | Not yet; rejected with a clear error |
| Network allowlists | Not yet; rejected |
| GPUs, Windows | No |
| Up to 16 vCPU, 64 GB memory, 100 GB disk per trial | Larger tasks are rejected, not shrunk |

## Cleanup

If a Harbor process is killed, its VMs still stop at `max_duration_sec`. To
end them sooner:

```bash
tenki-harbor sessions   # list active Harbor sessions
tenki-harbor cleanup    # terminate them
```

## Development

```bash
uv sync
uv run pytest                                 # unit tests, offline
TENKI_API_KEY=tk_... uv run pytest -m live    # creates real Tenki VMs
uv run ruff check . && uv run ruff format .
```
