"""Harbor environment backed by Tenki Sandbox VMs.

Each trial gets one Tenki session (a full Ubuntu VM). Tenki cannot boot OCI
images, so the task's container runs under Docker inside the VM: prebuilt
``docker_image`` tasks are pulled, Dockerfile tasks are built in the VM, and
every Harbor operation is routed to that container with ``docker exec`` /
``docker cp``.

Use with ``harbor run -e tenki_harbor:TenkiEnvironment``. Auth comes from
``TENKI_API_KEY`` (or ``TENKI_AUTH_TOKEN``).
"""

from __future__ import annotations

import asyncio
import math
import os
import posixpath
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any, override
from uuid import uuid4

from harbor.environments.base import BaseEnvironment, ExecResult, SandboxBuildFailedError
from harbor.environments.capabilities import (
    EnvironmentCapabilities,
    EnvironmentResourceCapabilities,
)
from harbor.environments.definition import (
    require_agent_environment_definition,
    should_use_prebuilt_docker_image,
)
from harbor.environments.tar_transfer import extract_dir_from_file, pack_dir_to_file
from harbor.models.task.config import NetworkMode, NetworkPolicy
from tenki import AsyncSandbox, CommandTimeoutError

CONTAINER = "main"
HARBOR_DIR = "/home/tenki/.harbor"
XFER_DIR = f"{HARBOR_DIR}/xfer"
BUILD_DIR = f"{HARBOR_DIR}/build"

TENKI_MAX_CPUS = 16
TENKI_MIN_MEMORY_MB = 512
TENKI_MAX_MEMORY_MB = 65536
TENKI_MIN_DISK_GB = 5
TENKI_MAX_DISK_GB = 100
DEFAULT_CPUS = 2
DEFAULT_MEMORY_MB = 4096
# Docker keeps both the compressed and the extracted layers, so a large
# image (Terminal-Bench's CUDA ones are ~9 GB compressed) needs ~3x its size.
DEFAULT_DISK_GB = 50
IMAGE_DISK_HEADROOM_GB = 30

DEFAULT_MAX_DURATION_SEC = 2 * 3600
READY_TIMEOUT_SEC = 300
CREATE_CANCEL_GRACE_SEC = 30
BOOTSTRAP_TIMEOUT_SEC = 600
CONTAINER_START_TIMEOUT_SEC = 120
TRANSFER_TIMEOUT_SEC = 600

# Docker hosts and Kubernetes nodes run with overcommit enabled; tasks that
# reserve large virtual allocations up front (e.g. a big malloc) fail without it.
DOCKER_BOOTSTRAP = r"""
set -e
sysctl -qw vm.overcommit_memory=1 || true
if ! command -v docker >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get -o DPkg::Lock::Timeout=180 update -qq
  apt-get -o DPkg::Lock::Timeout=180 install -y -qq --no-install-recommends \
    docker.io docker-buildx >/dev/null
fi
if ! docker info >/dev/null 2>&1; then
  systemctl start docker >/dev/null 2>&1 || (nohup dockerd >/var/log/dockerd.log 2>&1 &)
fi
for _ in $(seq 90); do
  docker info >/dev/null 2>&1 && exit 0
  sleep 1
done
echo "docker daemon did not become ready" >&2
exit 1
"""


def _session_name(session_id: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", session_id.lower()).strip("-")
    return name[:63].rstrip("-") or "harbor"


def _image_tag(environment_name: str) -> str:
    name = re.sub(r"[^a-z0-9._-]+", "-", environment_name.lower()).strip("-.")
    return f"hb__{name or 'task'}"


class TenkiEnvironment(BaseEnvironment):
    """Runs a Harbor task's container under Docker inside a Tenki VM.

    Environment kwargs (``--ek key=value``):

    - ``image`` / ``snapshot_id``: start the VM from a Tenki image or snapshot
      instead of the default base, e.g. one with Docker already installed.
    - ``disk_size_gb``: VM disk; defaults to the task's storage plus room for
      image layers.
    - ``max_duration_sec``: hard lifetime of the VM, so a crashed Harbor run
      cannot leak it. Must cover build + agent + verifier time.
    - ``base_url``: Tenki API endpoint override.
    """

    def __init__(
        self,
        *args: Any,
        image: str | None = None,
        snapshot_id: str | None = None,
        disk_size_gb: int | None = None,
        max_duration_sec: int = DEFAULT_MAX_DURATION_SEC,
        base_url: str | None = None,
        **kwargs: Any,
    ):
        if image and snapshot_id:
            raise ValueError("Pass at most one of image and snapshot_id.")
        self._tenki_image = image
        self._snapshot_id = snapshot_id
        self._disk_size_gb_override = disk_size_gb
        self._max_duration_sec = int(max_duration_sec)
        self._base_url = base_url
        self._sandbox: AsyncSandbox | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    @override
    def type() -> str:
        return "tenki"

    @property
    @override
    def capabilities(self) -> EnvironmentCapabilities:
        return EnvironmentCapabilities(disable_internet=True, dynamic_network_policy=True)

    @classmethod
    @override
    def resource_capabilities(cls) -> EnvironmentResourceCapabilities:
        # The VM is sized to the task, so its size is a hard ceiling.
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    @classmethod
    @override
    def preflight(cls) -> None:
        if not (os.environ.get("TENKI_API_KEY") or os.environ.get("TENKI_AUTH_TOKEN")):
            raise SystemExit(
                "Tenki requires TENKI_API_KEY (or TENKI_AUTH_TOKEN). "
                "Create a key at https://tenki.cloud/docs/account/api-keys."
            )

    @override
    def _validate_definition(self) -> None:
        if (self.environment_dir / "docker-compose.yaml").exists():
            raise ValueError(
                "Tenki does not support docker-compose tasks yet; "
                "only Dockerfile and docker_image tasks."
            )
        require_agent_environment_definition(
            self.environment_dir,
            docker_image=self.task_env_config.docker_image,
        )

    # ── Resources ────────────────────────────────────────────────────────

    def _vm_cpus(self) -> int:
        cpus = math.ceil(self._effective_cpus or DEFAULT_CPUS)
        if cpus > TENKI_MAX_CPUS:
            raise ValueError(f"Task asks for {cpus} CPUs; Tenki VMs have at most {TENKI_MAX_CPUS}.")
        return max(cpus, 1)

    def _vm_memory_mb(self) -> int:
        memory_mb = self._effective_memory_mb or DEFAULT_MEMORY_MB
        if memory_mb > TENKI_MAX_MEMORY_MB:
            raise ValueError(
                f"Task asks for {memory_mb} MB of memory; "
                f"Tenki VMs have at most {TENKI_MAX_MEMORY_MB} MB."
            )
        return max(memory_mb, TENKI_MIN_MEMORY_MB)

    def _vm_disk_gb(self) -> int:
        if self._disk_size_gb_override is not None:
            disk_gb = int(self._disk_size_gb_override)
        else:
            storage_mb = self._effective_storage_mb
            task_gb = math.ceil(storage_mb / 1024) if storage_mb else 0
            disk_gb = max(DEFAULT_DISK_GB, task_gb + IMAGE_DISK_HEADROOM_GB)
        if disk_gb > TENKI_MAX_DISK_GB:
            raise ValueError(
                f"Task needs a {disk_gb} GB disk; Tenki VMs have at most {TENKI_MAX_DISK_GB} GB."
            )
        return max(disk_gb, TENKI_MIN_DISK_GB)

    # ── Lifecycle ────────────────────────────────────────────────────────

    @override
    async def start(self, force_build: bool) -> None:
        await self._create_sandbox()
        await self._host_shell(
            f"mkdir -p {XFER_DIR} {BUILD_DIR}", timeout_sec=30, privileged=False, check=True
        )
        await self._host_shell(DOCKER_BOOTSTRAP, timeout_sec=BOOTSTRAP_TIMEOUT_SEC, check=True)

        docker_image = self.task_env_config.docker_image
        if docker_image and should_use_prebuilt_docker_image(
            self.environment_dir, docker_image=docker_image, force_build=force_build
        ):
            await self._pull_image(docker_image)
            image = docker_image
        else:
            image = await self._build_image()

        await self._run_container(image)
        if self.task_env_config.workdir:
            await self.exec(self._ensure_dirs_command([self.task_env_config.workdir], chmod=False))
        await self.ensure_dirs(self._mount_targets(writable_only=True))
        await self._upload_environment_dir_after_start()

    @override
    async def stop(self, delete: bool) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            return
        if not delete:
            self.logger.warning(
                f"Leaving Tenki session {sandbox.id} running because delete=False; "
                f"it terminates after max_duration_sec={self._max_duration_sec}."
            )
            return
        try:
            await sandbox.close_if_open()
        except Exception as exc:
            self.logger.warning(f"Failed to terminate Tenki session {sandbox.id}: {exc}")
        finally:
            self._sandbox = None

    async def _create_sandbox(self) -> None:
        create_kwargs: dict[str, Any] = {
            "name": _session_name(self.session_id),
            "cpu_cores": self._vm_cpus(),
            "memory_mb": self._vm_memory_mb(),
            "disk_size_gb": self._vm_disk_gb(),
            "max_duration": self._max_duration_sec,
            "metadata": {
                "harbor_session_id": self.session_id,
                "harbor_environment": self.environment_name,
            },
            "tags": ["harbor"],
            "wait": False,
        }
        if self._tenki_image:
            create_kwargs["image"] = self._tenki_image
        if self._snapshot_id:
            create_kwargs["snapshot_id"] = self._snapshot_id
        if self._base_url:
            create_kwargs["base_url"] = self._base_url

        # A cancelled trial still runs stop(); keep the handle so it can terminate the VM.
        create_task = asyncio.create_task(AsyncSandbox.create(**create_kwargs))
        try:
            self._sandbox = await asyncio.shield(create_task)
        except asyncio.CancelledError:
            try:
                self._sandbox = await asyncio.wait_for(create_task, CREATE_CANCEL_GRACE_SEC)
            except BaseException:
                create_task.cancel()
            raise
        self.logger.debug(f"Created Tenki session {self._sandbox.id}")
        await self._sandbox.wait_ready(timeout=READY_TIMEOUT_SEC)

    async def _pull_image(self, image: str) -> None:
        result = await self._host_exec(
            ["docker", "pull", "-q", image],
            timeout_sec=round(self.task_env_config.build_timeout_sec),
        )
        if result.return_code != 0:
            raise RuntimeError(f"docker pull {image} failed: {_tail(result)}")

    async def _build_image(self) -> str:
        tag = _image_tag(self.environment_name)
        staged = await self._stage_dir_archive(self.environment_dir)
        await self._host_shell(
            f"rm -rf {BUILD_DIR} && mkdir -p {BUILD_DIR} "
            f"&& tar -xf {shlex.quote(staged)} -C {BUILD_DIR} && rm -f {shlex.quote(staged)}",
            timeout_sec=TRANSFER_TIMEOUT_SEC,
            check=True,
        )
        result = await self._host_exec(
            ["docker", "build", "-t", tag, BUILD_DIR],
            timeout_sec=round(self.task_env_config.build_timeout_sec),
        )
        if result.return_code != 0:
            raise SandboxBuildFailedError(f"docker build failed: {_tail(result)}")
        return tag

    async def _run_container(self, image: str) -> None:
        await self._host_shell(f"docker rm -f {CONTAINER} >/dev/null 2>&1 || true", timeout_sec=60)
        result = await self._host_exec(
            ["docker", "run", "-d", "--name", CONTAINER, image, "sh", "-c", "sleep infinity"],
            timeout_sec=CONTAINER_START_TIMEOUT_SEC,
        )
        if result.return_code != 0:
            raise RuntimeError(f"docker run failed: {_tail(result)}")
        if self.network_policy.network_mode == NetworkMode.NO_NETWORK:
            await self._apply_network_policy(self.network_policy)

    @override
    async def _apply_network_policy(self, network_policy: NetworkPolicy) -> None:
        # The container starts on the default bridge; no-network detaches it,
        # leaving only loopback.
        match network_policy.network_mode:
            case NetworkMode.NO_NETWORK:
                argv = ["docker", "network", "disconnect", "bridge", CONTAINER]
            case NetworkMode.PUBLIC:
                argv = ["docker", "network", "connect", "bridge", CONTAINER]
            case _:
                raise ValueError(f"Tenki cannot enforce network_mode={network_policy.network_mode}")
        result = await self._host_exec(argv, timeout_sec=60)
        already = "is not connected" in (result.stderr or "") or "already exists" in (
            result.stderr or ""
        )
        if result.return_code != 0 and not already:
            raise RuntimeError(f"Failed to apply network policy: {_tail(result)}")

    # ── Exec ─────────────────────────────────────────────────────────────

    @override
    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        argv = ["docker", "exec"]
        if workdir := cwd or self.task_env_config.workdir:
            argv += ["-w", workdir]
        for key, value in (self._merge_env(env) or {}).items():
            argv += ["-e", f"{key}={value}"]
        if (resolved_user := self._resolve_user(user)) is not None:
            argv += ["-u", str(resolved_user)]
        argv += [CONTAINER, "bash", "-c", command]
        return await self._host_exec(argv, timeout_sec=timeout_sec)

    def _require_sandbox(self) -> AsyncSandbox:
        if self._sandbox is None:
            raise RuntimeError("Tenki session not started. Call start() first.")
        return self._sandbox

    async def _host_exec(
        self,
        argv: list[str],
        *,
        timeout_sec: int | None = None,
        privileged: bool = True,
    ) -> ExecResult:
        """Run argv on the VM itself (root by default, for Docker access)."""
        sandbox = self._require_sandbox()
        # Tenki applies a 30s default when no timeout is passed; Harbor's None means unbounded.
        budget = timeout_sec or self._max_duration_sec
        try:
            result = await sandbox.exec(*argv, timeout=budget, privileged=privileged)
        except CommandTimeoutError as exc:
            raise RuntimeError(f"Command timed out after {budget} seconds") from exc
        if result.timed_out:
            raise RuntimeError(f"Command timed out after {budget} seconds")
        return ExecResult(
            stdout=result.stdout_text or None,
            stderr=result.stderr_text or None,
            return_code=result.exit_code,
        )

    async def _host_shell(
        self,
        script: str,
        *,
        timeout_sec: int | None = None,
        privileged: bool = True,
        check: bool = False,
    ) -> ExecResult:
        result = await self._host_exec(
            ["bash", "-c", script], timeout_sec=timeout_sec, privileged=privileged
        )
        if check and result.return_code != 0:
            raise RuntimeError(f"Tenki VM command failed: {_tail(result)}")
        return result

    # ── File transfer ────────────────────────────────────────────────────
    #
    # Tenki's file API only reaches /home/tenki, so every transfer is two hops:
    # local <-> XFER_DIR on the VM (Tenki fs), then XFER_DIR <-> container (docker cp).

    def _staging_path(self, suffix: str = "") -> str:
        return f"{XFER_DIR}/{uuid4().hex}{suffix}"

    async def _stage_dir_archive(self, source_dir: Path | str) -> str:
        staged = self._staging_path(".tar")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "upload.tar"
            pack_dir_to_file(source_dir, archive, compress=False)
            await self._require_sandbox().fs.upload(archive, staged)
        return staged

    @override
    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        staged = self._staging_path()
        await self._require_sandbox().fs.upload(source_path, staged)
        parent = posixpath.dirname(target_path) or "/"
        await self._host_shell(
            f"docker exec {CONTAINER} mkdir -p {shlex.quote(parent)} "
            f"&& docker cp {shlex.quote(staged)} {CONTAINER}:{shlex.quote(target_path)}; "
            f"rc=$?; rm -f {shlex.quote(staged)}; exit $rc",
            timeout_sec=TRANSFER_TIMEOUT_SEC,
            check=True,
        )

    @override
    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        staged = await self._stage_dir_archive(source_dir)
        await self._host_shell(
            f"docker exec {CONTAINER} mkdir -p {shlex.quote(target_dir)} "
            f"&& docker cp - {CONTAINER}:{shlex.quote(target_dir)} < {shlex.quote(staged)}; "
            f"rc=$?; rm -f {shlex.quote(staged)}; exit $rc",
            timeout_sec=TRANSFER_TIMEOUT_SEC,
            check=True,
        )

    @override
    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        staged = self._staging_path()
        await self._host_shell(
            f"docker cp {CONTAINER}:{shlex.quote(source_path)} {shlex.quote(staged)} "
            f"&& chmod a+r {shlex.quote(staged)}",
            timeout_sec=TRANSFER_TIMEOUT_SEC,
            check=True,
        )
        try:
            Path(target_path).parent.mkdir(parents=True, exist_ok=True)
            await self._require_sandbox().fs.download(staged, target_path)
        finally:
            await self._host_shell(f"rm -f {shlex.quote(staged)}", timeout_sec=60)

    @override
    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        staged = self._staging_path(".tar")
        source = source_dir.rstrip("/") or "/"
        await self._host_shell(
            f"docker cp {CONTAINER}:{shlex.quote(source + '/.')} - > {shlex.quote(staged)}",
            timeout_sec=TRANSFER_TIMEOUT_SEC,
            check=True,
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "download.tar"
                await self._require_sandbox().fs.download(staged, archive)
                extract_dir_from_file(archive, target_dir)
        finally:
            await self._host_shell(f"rm -f {shlex.quote(staged)}", timeout_sec=60)


def _tail(result: ExecResult, limit: int = 2000) -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return output[-limit:] or f"exit code {result.return_code}"
