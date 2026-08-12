import asyncio

from runtime_api import NatsComm


async def main():
    comm = await NatsComm.create()
    try:
        while True:
            subject = input("subject: ").strip()
            if not subject:
                continue

            durable = input("durable name (press Enter for default, type NONE for no durable, type LAST to read last message): ").strip()
            if not durable:
                durable = subject.replace(".", "-")

            if durable == "NONE":
                durable = None
            elif durable == "LAST":
                message = await comm.receive_last(subject=subject)
                if message is None:
                    print("No last message found.")
                else:
                    print("Last message:", message.payload)
                continue

            messages = await comm.receive(
                subject=subject,
                durable=durable,
                batch=1,
                timeout_sec=10,
            )
            for message in messages:
                print("received:", message.payload)
                await message.ack()
    finally:
        await comm.close()


if __name__ == "__main__":
    asyncio.run(main())
