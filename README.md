# tenki-harbor

Run [Harbor](https://github.com/harbor-framework/harbor) agent evaluations on
[Tenki Sandbox](https://tenki.cloud/docs/sandbox) VMs.

Harbor runs coding-agent harnesses (Codex, OpenCode, Claude Code, mini-swe-agent,
Terminus, …) against graded benchmarks such as Terminal-Bench 2.0. This package
adds Tenki as a place to run those trials: every trial gets its own Tenki VM.

## Quickstart

```bash
uv tool install harbor \
  --with "tenki-harbor @ git+https://github.com/LuxorLabs/tenki-harbor" \
  --with-executables-from tenki-harbor
export TENKI_API_KEY=tk_...

# Sanity check with the reference solution (no model needed)
harbor run -t hello-world/hello-world \
  -e tenki_harbor:TenkiEnvironment --agent oracle
```

This installs the `harbor` CLI with the Tenki environment, plus the
`tenki-harbor` command for setup and cleanup. To use it from a Python project
instead: `uv add harbor "tenki-harbor @ git+https://github.com/LuxorLabs/tenki-harbor"`.

Run any Harbor agent on a benchmark:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
harbor run -d terminal-bench@2.0 -e tenki_harbor:TenkiEnvironment \
  --agent claude-code --model anthropic/claude-opus-4-1 \
  --n-concurrent 16 -l 10
```

To run one model across several harnesses and get a report of where it breaks,
use [tenki-harness-evals](https://github.com/LuxorLabs/tenki-harness-evals).
`examples/harness-matrix.yaml` in this repo is the same idea as a plain Harbor job config.

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

## Faster startup

Bake Docker into a Tenki snapshot once per workspace, and every trial skips the
Docker install (median setup on Terminal-Bench drops from 28 s to 19 s):

```bash
tenki-harbor prepare
export TENKI_HARBOR_SNAPSHOT_ID=<printed snapshot id>
```

## Terminal-Bench 2.0 results

Harbor's `oracle` agent (each task's reference solution) on Tenki production,
all 89 tasks, 16 trials at a time: **79 pass**. The other 10 fail for reasons in
the tasks themselves, not the environment:

| Reason | Tasks |
|---|---|
| Pinned apt/CRAN packages no longer published | `qemu-startup`, `qemu-alpine-ssh`, `make-doom-for-mips`, `build-pmars`, `rstan-to-pystan`, `mcmc-sampling-stan` |
| Oracle also fails on local Docker | `protein-assembly`, `build-cython-ext` |
| Upstream site returns 403 | `build-pov-ray` |
| Intermittent gloo hang in the full suite (each test passes on its own) | `torch-tensor-parallelism` |

## Options

Pass with `--ek key=value` or under `environment.kwargs` in a job config.

| Option | Default | |
|---|---|---|
| `max_duration_sec` | `7200` | Hard VM lifetime, so a crashed run can't leak a VM. Capped by the workspace limit. |
| `disk_size_gb` | task storage + 30 GB, min 50 | VM disk, which also holds the image layers. |
| `image` / `snapshot_id` | `TENKI_HARBOR_SNAPSHOT_ID`, else the Tenki base image | Start from a Tenki image or snapshot, e.g. one from `tenki-harbor prepare`. |
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
