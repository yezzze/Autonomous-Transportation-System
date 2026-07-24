import asyncio

from runtime_api import NatsComm


async def main():
    comm = NatsComm()
    try:
        reply = await comm.send_and_wait(
            subject="workflow.demo.agent.b.in",
            payload={
                "workflow_id": "external-app-1",
                "text": "hello from another container",
            },
            timeout_sec=120,
        )
        print("reply:", reply)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
