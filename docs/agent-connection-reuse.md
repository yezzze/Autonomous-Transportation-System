# 实际 Agent 的 NATS/gRPC 连接复用

## 生命周期原则

每个 Agent 进程创建一个 `NatsComm` 或 `FrameComm` 实例：

1. 进程启动时创建并连接。
2. 所有帧和任务共用该实例。
3. `serve()` 在进程生命周期内只启动一次。
4. 进程退出时调用 `close()`。

不要在单帧处理函数、HTTP handler、gRPC方法或模型推理函数内部创建通信实例，
也不要为每帧调用一次 `asyncio.run()`。

一个 Pod 如果启动4个worker进程，会建立4条NATS连接，这是正常的；连接不能跨进程
共享。一个进程内部的协程可以共享同一个实例，但该实例必须始终归属于同一个
asyncio事件循环。

## 异步 Agent

```python
import asyncio

from runtime_api import FrameComm


class Agent:
    def __init__(self):
        self.comm = FrameComm()

    async def start(self):
        await self.comm.nats.connect()

    async def infer(self, frame_path, workflow_id):
        return await self.comm.send_and_wait(
            subject="workflow.edge.agent.in",
            payload={"workflow_id": workflow_id},
            frame_path=frame_path,
            content_type="application/octet-stream",
            timeout_sec=120,
        )

    async def close(self):
        await self.comm.close()


async def main():
    agent = Agent()
    await agent.start()
    try:
        # HTTP/gRPC/MQ handler 都调用同一个 agent.infer()。
        await run_agent_server(agent)
    finally:
        await agent.close()


asyncio.run(main())
```

`FrameComm.send_and_wait()`会依次完成：

1. 通过复用的gRPC channel上传帧。
2. 只把`frame_ref`写入JetStream任务消息。
3. 自动创建不属于WORKFLOW Stream的`_INBOX`回复subject。
4. 收到一条Core NATS回复或超时后注销临时订阅。

业务代码不要再手工执行`send() + receive(durable=None)`。

## 消费型 Agent

```python
async def main():
    comm = FrameComm()

    async def handler(payload):
        frame_path = payload.get("frame_path")
        result = await run_model(frame_path)
        await comm.publish_core(
            payload["reply_subject"],
            {"workflow_id": payload["workflow_id"], "result": result},
        )

    try:
        await comm.serve(
            subject="workflow.edge.agent.in",
            durable="actual-agent-consumer",
            handler=handler,
            max_inflight=1,
            download_frames=True,
            delete_remote_frame=True,
        )
    finally:
        await comm.close()
```

`serve()`内部会缓存并复用同一个durable pull subscription。回复必须发布到请求
payload携带的`reply_subject`，不要自行拼接`workflow.*.reply.*`。

## 同步gRPC或线程模型

`nats-py`是异步客户端，连接不能在不同事件循环之间使用。同步服务器应建立一个
专用后台事件循环，在该循环中创建唯一的`FrameComm`，然后通过
`asyncio.run_coroutine_threadsafe()`提交任务。

不要在同步gRPC方法中这样处理：

```python
def Infer(request, context):
    comm = FrameComm()
    return asyncio.run(send_frame(comm, request))
```

参考实现见`agent_gRPC/server.py`中的`NatsRuntime`。

## 实际工程需要修改的位置

- 将本仓库最新的`runtime_api`同步到真实Agent镜像或公共Python包。
- 在Agent启动/关闭钩子中创建和关闭通信实例。
- 将逐帧`NatsComm()`或`FrameComm()`改为读取进程级实例。
- 将`send() + receive(durable=None)`改成`send_and_wait()`。
- 将回复端的`send(reply_subject, reply)`改成`publish_core()`。
- 保证回复端使用请求payload中的`reply_subject`。
- 多进程服务每个worker各持有一个实例，不要跨进程传递连接对象。

NATS Server和Helm配置不需要因为连接复用而修改。
