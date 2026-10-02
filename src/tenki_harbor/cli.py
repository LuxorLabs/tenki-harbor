"""Inspect and clean up the Tenki sessions Harbor runs leave behind."""

from __future__ import annotations

import argparse
import asyncio

from tenki import AsyncClient

HARBOR_TAG = "harbor"
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="tenki-harbor", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sessions", help="List active Tenki sessions started by Harbor.")
    commands.add_parser("cleanup", help="Terminate every active Tenki session started by Harbor.")
    args = parser.parse_args()
    handler = {"sessions": _list, "cleanup": _cleanup}[args.command]
    raise SystemExit(asyncio.run(handler()))
