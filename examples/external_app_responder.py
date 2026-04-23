import asyncio

from runtime_api import NatsComm


async def handle_request(payload):
    return {
        "status": "ok",
        "received": payload,
    }


async def main():
    comm = NatsComm()
    try:
        await comm.respond(
            subject="workflow.demo.request.status",
            handler=handle_request,
            queue="external-app-responders",
        )
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
