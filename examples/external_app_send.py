import asyncio

from runtime_api import NatsComm


async def main():
    comm = NatsComm()
    try:
        ack = await comm.send(
            subject="workflow.demo.agent.b.in",
            payload={
                "workflow_id": "external-app-1",
                "text": "hello from another container",
                "reply_subject": "workflow.demo.agent.grpc.reply.external-app-1",
            },
        )
        print("sent:", ack)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
