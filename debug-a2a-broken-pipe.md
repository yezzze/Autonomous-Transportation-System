# Debug Session: a2a-broken-pipe
- **Status**: [OPEN]
- **Issue**: `local_distributed_test.py` can reach the A2A `SendMessage` entrypoint, but both Collaborator and Ego tasks fail quickly with `Request failed: [Errno 32] Broken pipe`.
- **Debug Server**: pending
- **Log File**: `.dbg/trae-debug-log-a2a-broken-pipe.ndjson`

## Reproduction Steps
1. Run `python local_distributed_test.py` from the project root.
2. Wait for both agents to start and receive one A2A `SendMessage` request.
3. Observe the returned task status and error text.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | The failure happens inside `AgentTemplateExecutor.execute()` before `agent_function()` is entered. | High | Low | Pending |
| B | The failure is raised during runtime logging or stdout/stderr writes from the spawned uvicorn process. | High | Low | Pending |
| C | The failure happens during NATS publish/receive inside `NatsComm`, not at the A2A layer. | Medium | Medium | Pending |
| D | The failure is triggered by model runtime execution (`run_benchmark`) before/after the first frame. | Medium | Medium | Pending |
| E | The failure comes from the local test harness process management rather than agent business code. | Medium | Low | Pending |

## Log Evidence
- `.dbg/trae-debug-log-a2a-broken-pipe.ndjson`: `A` and `D` logs confirm the request reaches `AgentTemplateExecutor.execute()` and enters `agent_function()`.
- `/tmp/collab_uvicorn.log`: fresh startup run shows the real exception originates in `VOGS_Collaborator_Agent/fast_api/model_runtime.py` during priming, inside `deformable_aggregation_ext.deformable_aggregation_forward(...)`.
- `lsof -i :9001 -i :9002`: stale uvicorn processes from earlier runs were still bound to both ports, so part of the earlier `Broken pipe` observation came from requests hitting old agent processes instead of the latest code.

## Verification Conclusion
- Hypothesis A: **Rejected**. The request does enter `execute()` and `agent_function()`.
- Hypothesis B: **Rejected** as the primary root cause. The visible `Broken pipe` was not caused by stdout/stderr writes in the fresh run.
- Hypothesis C: **Rejected** for the current failure. The crash occurs before NATS publish.
- Hypothesis D: **Confirmed**. The real runtime failure is the collaborator CUDA custom op during the priming forward pass.
- Hypothesis E: **Confirmed** as a secondary issue. `local_distributed_test.py` left stale uvicorn processes alive, which made subsequent runs hit old services and masked the real error.
