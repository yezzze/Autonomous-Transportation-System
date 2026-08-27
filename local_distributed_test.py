import os
import sys
import time
import json
import subprocess
import requests
import atexit
import urllib.request
import signal

def run_command_in_bg(cmd, cwd=None, env=None):
    print(f"Running background command: {' '.join(cmd)} (cwd={cwd})")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=sys.stdout, stderr=sys.stderr)
    return proc


def _dbg(hypothesis_id, location, msg, data=None):
    try:
        event_url = "http://127.0.0.1:7777/event"
        session_id = "a2a-broken-pipe"
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dbg", "a2a-broken-pipe.env")
        with open(env_path, "r", encoding="utf-8") as env_file:
            for line in env_file:
                if line.startswith("DEBUG_SERVER_URL="):
                    event_url = line.split("=", 1)[1].strip()
                elif line.startswith("DEBUG_SESSION_ID="):
                    session_id = line.split("=", 1)[1].strip()
    except Exception:
        event_url = "http://127.0.0.1:7777/event"
        session_id = "a2a-broken-pipe"

    payload = {
        "sessionId": session_id,
        "runId": "pre-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "msg": msg,
        "data": data or {},
        "ts": int(time.time() * 1000),
    }
    try:
        req = urllib.request.Request(
            event_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def terminate_process_tree(proc, name):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    except ProcessLookupError:
        pass
    finally:
        # #region debug-point E:process-exit
        _dbg("E", "local_distributed_test.py:53", "[DEBUG] terminated agent process", {
            "name": name,
            "returncode": proc.returncode,
        })
        # #endregion

def wait_for_service(url, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(url)
            if resp.status_code == 200:
                print(f"Service at {url} is up!")
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(2)
    print(f"Timeout waiting for service at {url}")
    return False

def trigger_a2a_task(agent_url, parameters):
    payload = {
        "jsonrpc": "2.0",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "test-1",
                "role": "ROLE_USER",
                "parts": [
                    {
                        "data": {
                            "parameters": parameters
                        }
                    }
                ]
            }
        },
        "id": 1
    }
    
    print(f"\nSending A2A JSON-RPC to {agent_url}:")
    print(json.dumps(payload, indent=2))
    headers = {"A2A-Version": "1.0"}
    # #region debug-point E:request-dispatch
    _dbg("E", "local_distributed_test.py:76", "[DEBUG] dispatch A2A request", {
        "agent_url": agent_url,
        "headers": headers,
        "parameter_keys": sorted(parameters.keys()),
    })
    # #endregion
    resp = requests.post(agent_url, json=payload, headers=headers)
    # #region debug-point E:request-response
    _dbg("E", "local_distributed_test.py:83", "[DEBUG] received A2A response", {
        "agent_url": agent_url,
        "status_code": resp.status_code,
        "text_prefix": resp.text[:400],
    })
    # #endregion
    print(f"Response ({resp.status_code}):")
    print(json.dumps(resp.json(), indent=2))
    return resp.json()

def main():
    # 1. Start NATS Server (Assuming it's already started manually)
    print("Using existing NATS Server...")
    collab_proc = None
    ego_proc = None
    
    def cleanup():
        nonlocal collab_proc, ego_proc
        print("\nCleaning up...")
        terminate_process_tree(collab_proc, "collaborator")
        terminate_process_tree(ego_proc, "ego")

    atexit.register(cleanup)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 2. Setup environments for Agents
    # Note: We run them sequentially to avoid OOM on a single GPU.
    # To do this, we will start Collaborator, trigger it, wait for it, stop it.
    # Then start Ego, trigger it, wait for it, stop it.
    # Or, we just start them on the same GPU and hope they don't OOM if we trigger them sequentially?
    # Since they are two separate Uvicorn processes, if they both load the model, they will both consume VRAM!
    # GPU only has ~1GB free right now (23/24GB). They might crash during model loading.
    # Let's try to load them sequentially (start Collab -> Run -> Kill Collab -> Start Ego -> Run -> Kill Ego).
    
    collab_env = os.environ.copy()
    collab_env.update({
        "PYTHONUNBUFFERED": "1",
        "A2A_AGENT_URL": "http://localhost:9001",
        "NATS_SERVER_URL": "nats://127.0.0.1:4222",
        "CLUSTER_ID": "collab-cluster",
        "AGENT_ID": "vogs-collab",
        "AGENT_INSTANCE_ID": "collab-1",
        "NUM_FRAMES": "5",
        "CUDA_VISIBLE_DEVICES": "0"
    })
    
    uvicorn_bin = "/home/sxy/miniconda3/envs/vogs_dist/bin/uvicorn"
    
    print("\n--- Phase 1: Collaborator ---")
    collab_dir = os.path.join(base_dir, "VOGS_Collaborator_Agent")
    # #region debug-point B:spawn-collab
    _dbg("B", "local_distributed_test.py:126", "[DEBUG] spawning collaborator agent", {
        "cwd": collab_dir,
        "cmd": [uvicorn_bin, "fast_api.app:app", "--host", "0.0.0.0", "--port", "9001"],
    })
    # #endregion
    collab_proc = subprocess.Popen(
        [uvicorn_bin, "fast_api.app:app", "--host", "0.0.0.0", "--port", "9001"],
        cwd=collab_dir,
        env=collab_env,
        start_new_session=True,
    )
    
    if not wait_for_service("http://localhost:9001/.well-known/agent-card.json", timeout=60):
        sys.exit(1)
        
    print("Triggering Collaborator via A2A...")
    trigger_a2a_task("http://localhost:9001/", {
        "target_cluster": "ego-cluster",
        "target_agent_id": "vogs-ego",
        "target_instance_id": "ego-1",
        "operation": "in"
    })
    
    print("Collaborator finished. Terminating its process to free GPU memory...")
    terminate_process_tree(collab_proc, "collaborator")
    
    time.sleep(2) # Allow GPU memory to be freed
    
    print("\n--- Phase 2: Ego ---")
    ego_env = os.environ.copy()
    ego_env.update({
        "PYTHONUNBUFFERED": "1",
        "A2A_AGENT_URL": "http://localhost:9002",
        "NATS_SERVER_URL": "nats://127.0.0.1:4222",
        "CLUSTER_ID": "ego-cluster",
        "AGENT_ID": "vogs-ego",
        "AGENT_INSTANCE_ID": "ego-1",
        "NUM_FRAMES": "5",
        "CUDA_VISIBLE_DEVICES": "0"
    })
    
    ego_dir = os.path.join(base_dir, "VOGS_Ego_Agent")
    # #region debug-point B:spawn-ego
    _dbg("B", "local_distributed_test.py:161", "[DEBUG] spawning ego agent", {
        "cwd": ego_dir,
        "cmd": [uvicorn_bin, "fast_api.app:app", "--host", "0.0.0.0", "--port", "9002"],
    })
    # #endregion
    ego_proc = subprocess.Popen(
        [uvicorn_bin, "fast_api.app:app", "--host", "0.0.0.0", "--port", "9002"],
        cwd=ego_dir,
        env=ego_env,
        start_new_session=True,
    )
    
    if not wait_for_service("http://localhost:9002/.well-known/agent-card.json", timeout=60):
        sys.exit(1)
        
    print("Triggering Ego via A2A...")
    trigger_a2a_task("http://localhost:9002/", {
        "source_cluster": "collab-cluster",
        "operation": "in"
    })
    
    print("Ego finished.")
    # #region debug-point E:script-done
    _dbg("E", "local_distributed_test.py:177", "[DEBUG] local distributed test finished", {})
    # #endregion
    
    print("\nDistributed Test completed successfully!")

if __name__ == "__main__":
    main()
