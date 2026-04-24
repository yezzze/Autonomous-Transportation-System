import asyncio

from runtime_api import NatsComm


async def main():
    comm = NatsComm()
    try:
        messages = await comm.receive(
            subject="workflow.demo.agent.grpc.reply.*",
            durable="external-app-reply-consumer",
            batch=10,
            timeout_sec=10,
        )
        for message in messages:
            print("received:", message.payload)
            await message.ack()
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
