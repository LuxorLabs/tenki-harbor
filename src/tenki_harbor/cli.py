"""Prepare, inspect and clean up the Tenki sessions Harbor runs on."""

from __future__ import annotations

import argparse
import asyncio

from tenki import AsyncClient, AsyncSandbox

from tenki_harbor.environment import DOCKER_BOOTSTRAP, SNAPSHOT_ENV_VAR

HARBOR_TAG = "harbor"
# Restores can grow a snapshot's CPU, memory and disk but not shrink them, so
# build the base at the smallest size a trial could ask for.
BASE_CPUS = 1
BASE_MEMORY_MB = 1024
BASE_DISK_GB = 20
ACTIVE_STATES = {"CREATING", "RUNNING", "PAUSING", "PAUSED", "RESUMING"}


async def _active_sessions(client: AsyncClient):
    sessions = await client.list(tags=[HARBOR_TAG])
    return [s for s in sessions if s.info.state in ACTIVE_STATES]


async def _list() -> int:
    client = AsyncClient()
    sessions = await _active_sessions(client)
    for session in sessions:
        info = session.info
        print(
            f"{info.id}  {info.state:<9} {info.cpu_cores} vCPU {info.memory_mb} MB  "
            f"{info.metadata.get('harbor_session_id', info.name)}"
        )
    print(f"{len(sessions)} active Harbor session(s)")
    return 0


async def _cleanup() -> int:
    client = AsyncClient()
    sessions = await _active_sessions(client)
    failed = 0
    for session in sessions:
        try:
            await session.close()
            print(f"terminated {session.info.id}")
        except Exception as exc:
            failed += 1
            print(f"failed to terminate {session.info.id}: {exc}")
    print(f"terminated {len(sessions) - failed} of {len(sessions)} Harbor session(s)")
    return 1 if failed else 0


async def _prepare(name: str) -> int:
    sandbox = await AsyncSandbox.create(
        name=name,
        cpu_cores=BASE_CPUS,
        memory_mb=BASE_MEMORY_MB,
        disk_size_gb=BASE_DISK_GB,
        max_duration=1800,
        tags=["harbor-prepare"],
    )
    try:
        result = await sandbox.exec("bash", "-c", DOCKER_BOOTSTRAP, timeout=900, privileged=True)
        if result.exit_code != 0:
            print(f"Docker install failed:\n{result.stdout_text}{result.stderr_text}")
            return 1
        snapshot = await sandbox.snapshot(name=name)
    finally:
        await sandbox.close_if_open()
    # Wait until the snapshot can be restored on any host, not just the one it was taken on.
    snapshot = await AsyncClient().snapshots.wait_durable(snapshot.id, timeout=900)
    print(f"Snapshot {snapshot.id} is ready. Use it with:\n")
    print(f"  export {SNAPSHOT_ENV_VAR}={snapshot.id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="tenki-harbor", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare", help="Build a Tenki snapshot with Docker installed, for faster trial startup."
    )
    prepare.add_argument("--name", default="harbor-docker-base")
    commands.add_parser("sessions", help="List active Tenki sessions started by Harbor.")
    commands.add_parser("cleanup", help="Terminate every active Tenki session started by Harbor.")
    args = parser.parse_args()
    if args.command == "prepare":
        raise SystemExit(asyncio.run(_prepare(args.name)))
    handler = {"sessions": _list, "cleanup": _cleanup}[args.command]
    raise SystemExit(asyncio.run(handler()))
