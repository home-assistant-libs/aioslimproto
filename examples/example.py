"""aioslimproto example.."""
import asyncio
from os.path import abspath, dirname
from sys import path
import logging


path.insert(1, dirname(dirname(abspath(__file__))))

from aioslimproto import SlimServer
from aioslimproto.const import EventType

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
)


async def main():
    """Run code example."""
    server = SlimServer()
    await server.start()

    # subscribe to events
    async def on_event(evt, player):
        print(evt)
        # send play request to the first player that connects
        if evt == EventType.PLAYER_ADDED:
            await player.power(True)
            await player.volume_set(100)
            await player.play_url("http://192.168.1.109:8095/test.flac")

    server.subscribe(on_event)

    # wait a bit for some players to discover the server and connect
    await asyncio.sleep(3600)
    

try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
