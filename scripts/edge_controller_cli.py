#!/usr/bin/env python3
"""External orchestrator CLI for one in-cluster edge lifecycle controller."""

import argparse
import json
import os
import sys
from typing import Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def key_values(items) -> Dict[str, str]:
    result = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"expected KEY=VALUE: {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"empty key in {item!r}")
        result[key] = value
    return result


def request_json(
    method: str,
    base_url: str,
    path: str,
    token: Optional[str],
    body=None,
):
    data = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else None
    )
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=330) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {request.full_url} failed: HTTP {exc.code} {detail}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"{method} {request.full_url} failed: {exc.reason}"
        ) from exc


def print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def command_create(args) -> None:
    requests = {}
    limits = {}
    if args.cpu_request:
        requests["cpu"] = args.cpu_request
    if args.memory_request:
        requests["memory"] = args.memory_request
    if args.gpu is not None:
        requests["nvidia.com/gpu"] = str(args.gpu)
        limits["nvidia.com/gpu"] = str(args.gpu)
    if args.cpu_limit:
        limits["cpu"] = args.cpu_limit
    if args.memory_limit:
        limits["memory"] = args.memory_limit
    body = {
        "name": args.name,
        "namespace": args.namespace,
        "agent_id": args.agent_id,
        "image": args.image,
        "image_pull_policy": args.image_pull_policy,
        "env": key_values(args.env),
        "node_selector": key_values(args.node_selector),
        "resources": {"requests": requests, "limits": limits},
        "workflow_stream": not args.no_workflow_stream,
        "frame_stream": not args.no_frame_stream,
        "wait_ready_timeout_sec": args.wait_ready_timeout_sec,
    }
    print_json(
        request_json(
            "POST",
            args.base_url,
            "/v1/instances",
            args.token,
            body,
        )
    )


def command_get(args) -> None:
    print_json(
        request_json(
            "GET",
            args.base_url,
            f"/v1/instances/{args.namespace}/{args.name}",
            args.token,
        )
    )


def command_list(args) -> None:
    query = f"?{urlencode({'namespace': args.namespace})}" if args.namespace else ""
    print_json(
        request_json(
            "GET",
            args.base_url,
            f"/v1/instances{query}",
            args.token,
        )
    )


def command_delete(args) -> None:
    query = {
        "drain_timeout_sec": args.drain_timeout_sec,
        "force": str(args.force).lower(),
        "pod_grace_period_seconds": args.pod_grace_period_seconds,
    }
    if args.instance_id:
        query["instance_id"] = args.instance_id
    print_json(
        request_json(
            "DELETE",
            args.base_url,
            f"/v1/instances/{args.namespace}/{args.name}?{urlencode(query)}",
            args.token,
        )
    )


def command_simple(args) -> None:
    method, path = {
        "health": ("GET", "/v1/cluster/health"),
        "ready": ("GET", "/readyz"),
        "reconcile": ("POST", "/v1/reconcile"),
        "resources": ("GET", "/v1/nodes/resources"),
    }[args.command]
    print_json(request_json(method, args.base_url, path, args.token))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Agent Pods and instance Streams through an edge controller"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "EDGE_CONTROLLER_URL",
            "http://127.0.0.1:30080",
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("EDGE_CONTROLLER_TOKEN"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--namespace", default="default")
    create.add_argument("--agent-id", required=True)
    create.add_argument("--image", required=True)
    create.add_argument(
        "--image-pull-policy",
        choices=["Always", "IfNotPresent", "Never"],
        default="IfNotPresent",
    )
    create.add_argument("--env", action="append", default=[])
    create.add_argument("--node-selector", action="append", default=[])
    create.add_argument("--cpu-request")
    create.add_argument("--cpu-limit")
    create.add_argument("--memory-request")
    create.add_argument("--memory-limit")
    create.add_argument("--gpu", type=int)
    create.add_argument("--no-workflow-stream", action="store_true")
    create.add_argument("--no-frame-stream", action="store_true")
    create.add_argument("--wait-ready-timeout-sec", type=float, default=0)
    create.set_defaults(func=command_create)

    get = subparsers.add_parser("get")
    get.add_argument("name")
    get.add_argument("--namespace", default="default")
    get.set_defaults(func=command_get)

    listing = subparsers.add_parser("list")
    listing.add_argument("--namespace")
    listing.set_defaults(func=command_list)

    delete = subparsers.add_parser("delete")
    delete.add_argument("name")
    delete.add_argument("--namespace", default="default")
    delete.add_argument("--instance-id")
    delete.add_argument("--drain-timeout-sec", type=float, default=30)
    delete.add_argument("--pod-grace-period-seconds", type=int, default=30)
    delete.add_argument("--force", action="store_true")
    delete.set_defaults(func=command_delete)

    for name in ("health", "ready", "reconcile", "resources"):
        command = subparsers.add_parser(name)
        command.set_defaults(func=command_simple)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.token and args.command != "ready":
        print(
            "error: set EDGE_CONTROLLER_TOKEN or pass --token",
            file=sys.stderr,
        )
        return 2
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
