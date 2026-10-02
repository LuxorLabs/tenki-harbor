"""Live tests against Tenki production.

Run with ``TENKI_API_KEY=... uv run pytest -m live``. Each test creates and
terminates real VMs.
"""

import os
from pathlib import Path

import pytest
from harbor.models.task.config import EnvironmentConfig, NetworkMode, NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from tenki import AsyncClient

from tenki_harbor import TenkiEnvironment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("TENKI_API_KEY"), reason="TENKI_API_KEY not set"),
]

# A real Terminal-Bench 2.0 image.
TB2_IMAGE = "alexgshaw/break-filter-js-from-html:20251031"


def make_env(
    tmp_path: Path,
    name: str,
    *,
    dockerfile: str | None = None,
    network_policy: NetworkPolicy | None = None,
    **config,
) -> TenkiEnvironment:
    env_dir = tmp_path / "environment"
    env_dir.mkdir()
    if dockerfile:
        (env_dir / "Dockerfile").write_text(dockerfile)
    trial_paths = TrialPaths(tmp_path / "trial")
    trial_paths.mkdir()
    return TenkiEnvironment(
        environment_dir=env_dir,
        environment_name=name,
        session_id=f"live-{name}",
        trial_paths=trial_paths,
        task_env_config=EnvironmentConfig(cpus=1, memory_mb=2048, **config),
        network_policy=network_policy,
        max_duration_sec=1800,
    )


async def assert_terminated(session_id: str) -> None:
    sessions = await AsyncClient().list(tags=["harbor"], include_terminated=True)
    states = {s.info.id: s.info.state for s in sessions}
    assert states.get(session_id) in {"TERMINATING", "TERMINATED"}


async def test_prebuilt_image_lifecycle_and_transfers(tmp_path):
    env = make_env(tmp_path, "prebuilt", docker_image=TB2_IMAGE)
    await env.start(force_build=False)
    session_id = env._sandbox.id
    try:
        result = await env.exec("pwd && id -u")
        assert result.return_code == 0
        assert result.stdout.split() == ["/app", "0"]

        result = await env.exec("echo $GREETING", env={"GREETING": "hi"}, cwd="/tmp")
        assert result.stdout.strip() == "hi"

        local_dir = tmp_path / "upload"
        (local_dir / "nested").mkdir(parents=True)
        (local_dir / "nested" / "run.sh").write_text("#!/bin/sh\necho ran\n")
        (local_dir / "nested" / "run.sh").chmod(0o755)
        await env.upload_dir(local_dir, "/opt/harbor-test")
        result = await env.exec("/opt/harbor-test/nested/run.sh")
        assert result.stdout.strip() == "ran"

        await env.exec("mkdir -p /logs/out && echo result > /logs/out/a.txt")
        download = tmp_path / "download"
        await env.download_dir("/logs/out", download)
        assert (download / "a.txt").read_text().strip() == "result"

        await env.download_file("/logs/out/a.txt", tmp_path / "single.txt")
        assert (tmp_path / "single.txt").read_text().strip() == "result"

        assert await env.is_dir("/opt/harbor-test")
        assert await env.is_file("/opt/harbor-test/nested/run.sh")
    finally:
        await env.stop(delete=True)
    await assert_terminated(session_id)


async def test_dockerfile_task_is_built(tmp_path):
    env = make_env(
        tmp_path,
        "dockerfile",
        dockerfile="FROM python:3.12-slim\nRUN echo built > /proof\nWORKDIR /work\n",
    )
    await env.start(force_build=False)
    try:
        result = await env.exec("cat /proof && pwd")
        assert result.stdout.split() == ["built", "/work"]
    finally:
        await env.stop(delete=True)


async def test_exec_timeout_raises(tmp_path):
    env = make_env(tmp_path, "timeout", docker_image=TB2_IMAGE)
    await env.start(force_build=False)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await env.exec("sleep 30", timeout_sec=3)
    finally:
        await env.stop(delete=True)


async def test_network_policy_switches_at_runtime(tmp_path):
    no_network = NetworkPolicy(network_mode=NetworkMode.NO_NETWORK)
    env = make_env(tmp_path, "network", docker_image="python:3.12-slim", network_policy=no_network)
    probe = "python3 -c \"import urllib.request as u; u.urlopen('https://pypi.org', timeout=5)\""
    await env.start(force_build=False)
    try:
        assert (await env.exec(probe)).return_code != 0
        await env.set_network_policy(NetworkPolicy(network_mode=NetworkMode.PUBLIC))
        assert (await env.exec(probe)).return_code == 0
        await env.set_network_policy(no_network)
        assert (await env.exec(probe)).return_code != 0
    finally:
        await env.stop(delete=True)
