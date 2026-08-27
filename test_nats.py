import asyncio
import nats
from nats.errors import NoRespondersError
async def main():
    nc = await nats.connect("nats://127.0.0.1:4222")
    js = nc.jetstream()
    try:
        info = await js.stream_info("TEST")
        print(info)
    except NoRespondersError:
        print("NoRespondersError")
    except Exception as e:
        print(type(e), e)
    await nc.close()
asyncio.run(main())
