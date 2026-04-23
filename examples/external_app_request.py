import asyncio

from runtime_api import NatsComm


async def main():
    comm = NatsComm()
    try:
        reply = await comm.request(
            subject="workflow.demo.request.status",
            payload={"text": "hello from request api"},
            timeout_sec=30,
        )
        print("reply:", reply)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
