#!/usr/bin/env python3
"""编排器管理实例级 JetStream Stream 的命令行入口。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime_api import NatsComm


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision or delete one Agent instance workflow Stream",
    )
    parser.add_argument(
        "--server",
        action="append",
        dest="servers",
        help="local NATS URL; repeat for multiple servers",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision = subparsers.add_parser(
        "provision",
        help="create or reconcile one instance Stream",
    )
    provision.add_argument("--cluster", required=True)
    provision.add_argument("--agent", required=True)
    provision.add_argument("--instance", required=True)

    delete = subparsers.add_parser(
        "delete",
        help="delete one finished instance Stream",
    )
    delete.add_argument("--cluster", required=True)
    delete.add_argument("--instance", required=True)
    return parser


async def run(args: argparse.Namespace) -> None:
    comm = NatsComm(servers=args.servers)
    try:
        if args.command == "provision":
            result = await comm.provision_workflow_stream(
                target_cluster=args.cluster,
                agent_id=args.agent,
                instance_id=args.instance,
            )
        else:
            deleted = await comm.delete_workflow_stream(
                target_cluster=args.cluster,
                instance_id=args.instance,
            )
            result = {
                "stream": comm.workflow_stream_name(args.instance),
                "domain": args.cluster,
                "instance_id": args.instance,
                "deleted": deleted,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        await comm.close()


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
