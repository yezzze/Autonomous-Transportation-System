import asyncio
from datetime import datetime

from runtime_api import NatsComm


async def main():
    comm = NatsComm()
    try:
        while True:
            subject = input("subject: ").strip()
            if not subject:
                continue

            text = input("text: ")
            ack = await comm.send(
                subject=subject,
                payload={
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "text": text,
                },
            )
            print("sent:", ack)
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
