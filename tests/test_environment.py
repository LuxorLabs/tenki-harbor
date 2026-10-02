import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths

import tenki_harbor.environment as tenki_env
from tenki_harbor import TenkiEnvironment


@dataclass
class FakeResult:
    exit_code: int = 0
    stdout_text: str = ""
    stderr_text: str = ""
    timed_out: bool = False


class FakeFs:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []

    async def upload(self, local_path, remote_path):
        self.uploads.append((str(local_path), remote_path))

    async def download(self, remote_path, local_path):
        Path(local_path).write_text("downloaded")


class FakeSandbox:
    created: list["FakeSandbox"] = []
    create_kwargs: dict = {}
    create_delay: float = 0

    def __init__(self):
        self.id = "sess-1"
        self.calls: list[dict] = []
        self.fs = FakeFs()
        self.closed = False
        self.result_for = lambda argv: FakeResult()

    @classmethod
    async def create(cls, **kwargs):
        cls.create_kwargs = kwargs
        if cls.create_delay:
            await asyncio.sleep(cls.create_delay)
        sandbox = cls()
        cls.created.append(sandbox)
        return sandbox

    async def wait_ready(self, timeout):
        pass

    async def exec(self, *argv, timeout, privileged):
        self.calls.append({"argv": list(argv), "timeout": timeout, "privileged": privileged})
        return self.result_for(list(argv))

    async def close_if_open(self):
        self.closed = True

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls]


@pytest.fixture(autouse=True)
def fake_tenki(monkeypatch):
    FakeSandbox.created = []
    FakeSandbox.create_kwargs = {}
    FakeSandbox.create_delay = 0
    monkeypatch.setattr(tenki_env, "AsyncSandbox", FakeSandbox)


def make_env(
    tmp_path: Path,
    *,
    dockerfile: str | None = "FROM ubuntu:24.04\n",
    compose: bool = False,
    network_policy: NetworkPolicy | None = None,
    persistent_env: dict[str, str] | None = None,
    env_kwargs: dict | None = None,
    **config,
) -> TenkiEnvironment:
    env_dir = tmp_path / "environment"
    env_dir.mkdir(parents=True)
    if dockerfile is not None:
        (env_dir / "Dockerfile").write_text(dockerfile)
    if compose:
        (env_dir / "docker-compose.yaml").write_text("services: {}\n")
    trial_paths = TrialPaths(tmp_path / "trial")
    trial_paths.mkdir()
    return TenkiEnvironment(
        environment_dir=env_dir,
        environment_name="My Task",
        session_id="my-task__abc123__env",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(**config),
        network_policy=network_policy,
        persistent_env=persistent_env,
        **(env_kwargs or {}),
    )


async def started(env: TenkiEnvironment, force_build: bool = False) -> FakeSandbox:
    await env.start(force_build=force_build)
    return FakeSandbox.created[-1]


def test_vm_resources_follow_task(tmp_path):
    env = make_env(tmp_path, cpus=3, memory_mb=256, storage_mb=30 * 1024)
    assert env._vm_cpus() == 3
    assert env._vm_memory_mb() == tenki_env.TENKI_MIN_MEMORY_MB
    assert env._vm_disk_gb() == 30 + tenki_env.IMAGE_DISK_HEADROOM_GB


def test_vm_disk_has_a_floor_and_can_be_overridden(tmp_path):
    assert make_env(tmp_path / "a", storage_mb=1024)._vm_disk_gb() == tenki_env.DEFAULT_DISK_GB
    env = make_env(tmp_path / "b", env_kwargs={"disk_size_gb": 50})
    assert env._vm_disk_gb() == 50


def test_oversized_task_is_rejected_rather_than_shrunk(tmp_path):
    with pytest.raises(ValueError, match="at most 16"):
        make_env(tmp_path, cpus=32)._vm_cpus()


def test_compose_tasks_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="docker-compose"):
        make_env(tmp_path, compose=True)


def test_allowlist_network_is_rejected(tmp_path):
    policy = NetworkPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["pypi.org"])
    with pytest.raises(ValueError, match="allowlist"):
        make_env(tmp_path, network_policy=policy)


def test_preflight_requires_api_key(monkeypatch):
    monkeypatch.delenv("TENKI_API_KEY", raising=False)
    monkeypatch.delenv("TENKI_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="TENKI_API_KEY"):
        TenkiEnvironment.preflight()
    monkeypatch.setenv("TENKI_API_KEY", "tk_test")
    TenkiEnvironment.preflight()


async def test_prebuilt_image_is_pulled_not_built(tmp_path):
    env = make_env(tmp_path, docker_image="alexgshaw/task:1", cpus=1, memory_mb=2048)
    sandbox = await started(env)

    argvs = sandbox.argvs()
    assert ["docker", "pull", "-q", "alexgshaw/task:1"] in argvs
    assert not any(argv[:2] == ["docker", "build"] for argv in argvs)
    run = next(argv for argv in argvs if argv[:2] == ["docker", "run"])
    assert run[-4:] == ["alexgshaw/task:1", "sh", "-c", "sleep infinity"]
    assert FakeSandbox.create_kwargs["cpu_cores"] == 1
    assert FakeSandbox.create_kwargs["memory_mb"] == 2048
    assert FakeSandbox.create_kwargs["tags"] == ["harbor"]
    assert FakeSandbox.create_kwargs["name"] == "my-task-abc123-env"


async def test_dockerfile_task_is_built_in_the_vm(tmp_path):
    env = make_env(tmp_path)
    sandbox = await started(env)

    build = next(argv for argv in sandbox.argvs() if argv[:2] == ["docker", "build"])
    assert build == ["docker", "build", "-t", "hb__my-task", tenki_env.BUILD_DIR]
    run = next(argv for argv in sandbox.argvs() if argv[:2] == ["docker", "run"])
    assert "hb__my-task" in run


async def test_force_build_ignores_prebuilt_image(tmp_path):
    env = make_env(tmp_path, docker_image="alexgshaw/task:1")
    sandbox = await started(env, force_build=True)
    assert any(argv[:2] == ["docker", "build"] for argv in sandbox.argvs())


async def test_no_network_detaches_the_container(tmp_path):
    env = make_env(tmp_path, network_policy=NetworkPolicy(network_mode=NetworkMode.NO_NETWORK))
    sandbox = await started(env)
    assert ["docker", "network", "disconnect", "bridge", "main"] in sandbox.argvs()


async def test_exec_runs_in_the_container_as_root_on_the_vm(tmp_path):
    env = make_env(tmp_path, workdir="/app", persistent_env={"A": "1"})
    sandbox = await started(env)
    sandbox.calls.clear()

    await env.exec("echo hi", env={"B": "2"}, user="agent", timeout_sec=5)

    call = sandbox.calls[-1]
    assert call["argv"] == [
        "docker", "exec", "-w", "/app", "-e", "A=1", "-e", "B=2", "-u", "agent",
        "main", "bash", "-c", "echo hi",
    ]  # fmt: skip
    assert call["timeout"] == 5
    assert call["privileged"] is True


async def test_exec_without_timeout_uses_session_budget(tmp_path):
    env = make_env(tmp_path, env_kwargs={"max_duration_sec": 900})
    sandbox = await started(env)
    await env.exec("true")
    assert sandbox.calls[-1]["timeout"] == 900


async def test_exec_timeout_raises(tmp_path):
    env = make_env(tmp_path)
    sandbox = await started(env)
    sandbox.result_for = lambda argv: FakeResult(exit_code=-1, timed_out=True)
    with pytest.raises(RuntimeError, match="timed out"):
        await env.exec("sleep 100", timeout_sec=1)


async def test_upload_file_stages_under_tenki_home(tmp_path):
    env = make_env(tmp_path)
    sandbox = await started(env)
    local = tmp_path / "file.txt"
    local.write_text("x")

    await env.upload_file(local, "/app/file.txt")

    _, staged = sandbox.fs.uploads[-1]
    assert staged.startswith(tenki_env.XFER_DIR + "/")
    script = sandbox.argvs()[-1][-1]
    assert f"docker cp {staged} main:/app/file.txt" in script


async def test_stop_terminates_only_when_deleting(tmp_path):
    env = make_env(tmp_path)
    sandbox = await started(env)
    await env.stop(delete=False)
    assert not sandbox.closed
    await env.stop(delete=True)
    assert sandbox.closed


async def test_cancelled_create_keeps_handle_for_cleanup(tmp_path):
    FakeSandbox.create_delay = 0.05
    env = make_env(tmp_path)
    task = asyncio.create_task(env.start(force_build=False))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await env.stop(delete=True)
    assert FakeSandbox.created[-1].closed


async def test_snapshot_comes_from_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(tenki_env.SNAPSHOT_ENV_VAR, "snap-1")
    await started(make_env(tmp_path / "a"))
    assert FakeSandbox.create_kwargs["snapshot_id"] == "snap-1"

    await started(make_env(tmp_path / "b", env_kwargs={"image": "acme/base"}))
    assert "snapshot_id" not in FakeSandbox.create_kwargs
    assert FakeSandbox.create_kwargs["image"] == "acme/base"
