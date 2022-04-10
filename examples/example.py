"""aioslimproto example.."""
import asyncio
from os.path import abspath, dirname
from sys import path
import logging


path.insert(1, dirname(dirname(abspath(__file__))))

from aioslimproto import SlimServer
from aioslimproto.const import EventType, SlimEvent

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
)


async def main():
    """Run code example."""
    server = SlimServer()
    await server.start()

    # subscribe to events
    async def on_event(evt: SlimEvent):
        print(f"Received event: {evt}")
        # send request to a player
        if evt.type == EventType.PLAYER_CONNECTED:
            player = server.get_player(evt.player_id)
            await player.power(True)
            await player.volume_set(100)
            # await player.play_url(
            #     "http://playerservices.streamtheworld.com/api/livestream-redirect/TLPSTR13AAC.aac"
            # )

    server.subscribe(on_event)

    # wait a bit for some players to discover the server and connect
    await asyncio.sleep(3600)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
