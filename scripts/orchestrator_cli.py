#!/usr/bin/env python3
import argparse
import json
import sys
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(method: str, base_url: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = Request(url=url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc


def print_json(data: Dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_install(args) -> None:
    body = {
        "name": args.name,
        "task_description": args.task,
        "orchestration_mode": args.mode,
        "agents_required": args.agents,
        "constraints": {
            "timeout_seconds": args.timeout,
        },
    }
    result = request_json("POST", args.base_url, "/api/apps/install", body)
    print_json(result)


def cmd_start(args) -> None:
    body = {
        "resource_config": {
            "cpu_cores": args.cpu,
            "memory_mb": args.memory_mb,
            "gpu_count": args.gpu,
            "node_id": args.node_id,
        }
    }
    result = request_json("POST", args.base_url, f"/api/apps/{args.app_id}/start", body)
    print_json(result)


def cmd_run_demo(args) -> None:
    install_body = {
        "name": args.name,
        "task_description": args.task,
        "orchestration_mode": args.mode,
        "agents_required": args.agents,
        "constraints": {
            "timeout_seconds": args.timeout,
        },
    }
    install_result = request_json("POST", args.base_url, "/api/apps/install", install_body)
    app_id = install_result["app_id"]
    print("installed:")
    print_json(install_result)

    start_body = {
        "resource_config": {
            "cpu_cores": args.cpu,
            "memory_mb": args.memory_mb,
            "gpu_count": args.gpu,
            "node_id": args.node_id,
        }
    }
    start_result = request_json("POST", args.base_url, f"/api/apps/{app_id}/start", start_body)
    print("started:")
    print_json(start_result)
    print("note: start only deploys the app instance; use the interface endpoint to execute a workflow.")


def cmd_k8s_demo(args) -> None:
    agents_required = ["agent-b", "agent-c"]
    images = []
    if getattr(args, "include_agent_grpc", False):
        agents_required.insert(0, "agent-grpc")
        images.append(
            {
                "image_id": args.agent_grpc_image,
                "name": "agent-grpc",
                "version": args.agent_grpc_image.split(":")[-1] if ":" in args.agent_grpc_image else "latest",
                "capability": "agent-grpc",
                "description": "gRPC entrypoint that publishes work to NATS and returns replies",
            }
        )
    images.extend(
        [
            {
                "image_id": args.agent_b_image,
                "name": "agent-b",
                "version": args.agent_b_image.split(":")[-1] if ":" in args.agent_b_image else "latest",
                "capability": "agent-b",
                "description": "NATS worker that forwards work to agent-c and replies to agent-grpc",
            },
            {
                "image_id": args.agent_c_image,
                "name": "agent-c",
                "version": args.agent_c_image.split(":")[-1] if ":" in args.agent_c_image else "latest",
                "capability": "agent-c",
                "description": "NATS worker that processes requests from agent-b",
            },
        ]
    )
    install_body = {
        "name": args.name,
        "task_description": "启动 agent-grpc/agent-b/agent-c，通过 gRPC + NATS 完成通信",
        "orchestration_mode": "adaptive",
        "agents_required": agents_required,
        "constraints": {
            "timeout_seconds": args.timeout,
        },
        "images": images,
    }
    install_result = request_json("POST", args.base_url, "/api/apps/install", install_body)
    app_id = install_result["app_id"]
    print("installed:")
    print_json(install_result)

    start_body = {
        "resource_config": {
            "cpu_cores": args.cpu,
            "memory_mb": args.memory_mb,
            "gpu_count": args.gpu,
            "node_id": args.node_id,
        }
    }
    start_result = request_json("POST", args.base_url, f"/api/apps/{app_id}/start", start_body)
    print("started:")
    print_json(start_result)
    if getattr(args, "include_agent_grpc", False):
        print("next: run python client.py against agent_gRPC on localhost:50051 or NodePort 30051")
    else:
        print("next: start your remote agent_gRPC entry, then run python client.py")


def cmd_stop(args) -> None:
    result = request_json("POST", args.base_url, f"/api/apps/{args.app_id}/stop")
    print_json(result)


def cmd_apps(args) -> None:
    print_json(request_json("GET", args.base_url, "/api/apps/"))


def cmd_running(args) -> None:
    print_json(request_json("GET", args.base_url, "/api/apps/running"))


def cmd_deployments(args) -> None:
    print_json(request_json("GET", args.base_url, "/api/agents/deployments"))


def add_resource_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cpu", type=float, default=1.0, help="CPU cores, default: 1.0")
    parser.add_argument("--memory-mb", type=int, default=1024, help="Memory in MB, default: 1024")
    parser.add_argument("--gpu", type=int, default=0, help="GPU count, default: 0")
    parser.add_argument("--node-id", default="localhost", help="Target node id, default: localhost")


def add_install_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default="k8s-nats-demo-app", help="Application name")
    parser.add_argument("--task", default="启动一个使用 NATS 进行 Pod 间通信的应用", help="Task description")
    parser.add_argument("--mode", default="adaptive", help="Orchestration mode")
    parser.add_argument("--agents", nargs="+", default=["demo"], help="Required agent capabilities")
    parser.add_argument("--timeout", type=int, default=120, help="Workflow timeout seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convenience CLI for the Autonomous Transportation orchestrator API")
    parser.add_argument("--base-url", default="http://localhost:8001", help="Orchestrator API base URL")
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="Install an app")
    add_install_args(install)
    install.set_defaults(func=cmd_install)

    start = sub.add_parser("start", help="Start an app with resource config")
    start.add_argument("app_id", help="Application id")
    add_resource_args(start)
    start.set_defaults(func=cmd_start)

    run_demo = sub.add_parser("run-demo", help="Install and start a demo app")
    add_install_args(run_demo)
    add_resource_args(run_demo)
    run_demo.set_defaults(func=cmd_run_demo)

    k8s_demo = sub.add_parser(
        "k8s-demo",
        aliases=["agents-bc"],
        help="Install and start the agent-b/agent-c NATS workers through the orchestrator",
    )
    k8s_demo.add_argument("--name", default="k8s-nats-bc-demo", help="Application name")
    k8s_demo.add_argument("--agent-b-image", default="agent-b-worker:v3", help="Agent B image")
    k8s_demo.add_argument("--agent-c-image", default="agent-c-worker:v1", help="Agent C image")
    k8s_demo.add_argument("--timeout", type=int, default=120, help="Workflow timeout seconds")
    add_resource_args(k8s_demo)
    k8s_demo.set_defaults(func=cmd_k8s_demo)

    agents_abc = sub.add_parser("agents-abc", help="Install and start agent-grpc/agent-b/agent-c through the orchestrator")
    agents_abc.add_argument("--name", default="k8s-nats-abc-demo", help="Application name")
    agents_abc.add_argument("--agent-grpc-image", "--agent-a-image", dest="agent_grpc_image", default="agent-grpc:v1", help="agent_gRPC image")
    agents_abc.add_argument("--agent-b-image", default="agent-b-worker:v3", help="Agent B image")
    agents_abc.add_argument("--agent-c-image", default="agent-c-worker:v1", help="Agent C image")
    agents_abc.add_argument("--timeout", type=int, default=120, help="Workflow timeout seconds")
    agents_abc.set_defaults(include_agent_grpc=True)
    add_resource_args(agents_abc)
    agents_abc.set_defaults(func=cmd_k8s_demo)

    stop = sub.add_parser("stop", help="Stop an app")
    stop.add_argument("app_id", help="Application id")
    stop.set_defaults(func=cmd_stop)

    apps = sub.add_parser("apps", help="List apps")
    apps.set_defaults(func=cmd_apps)

    running = sub.add_parser("running", help="List running apps")
    running.set_defaults(func=cmd_running)

    deployments = sub.add_parser("deployments", help="List running agent deployments")
    deployments.set_defaults(func=cmd_deployments)

    return parser


def normalize_legacy_args(argv):
    if not argv:
        return argv

    known_commands = {"install", "start", "run-demo", "k8s-demo", "agents-bc", "agents-abc", "stop", "apps", "running", "deployments"}
    global_options = {"-h", "--help", "--base-url"}
    if argv[0] in known_commands or argv[0] in global_options:
        return argv

    # Convenience form:
    #   ./orchestrator_cli.py agent-b-worker --cpu 1 --memory-mb 1024
    # means:
    #   ./orchestrator_cli.py run-demo --agents agent-b-worker --name agent-b-worker
    app_name = argv[0]
    return ["run-demo", "--name", app_name, "--agents", app_name] + argv[1:]


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_legacy_args(sys.argv[1:]))
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        args.func(args)
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
