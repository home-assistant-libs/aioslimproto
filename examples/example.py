"""aioslimproto example.."""
import asyncio
from os.path import abspath, dirname
from sys import path
import logging


path.insert(1, dirname(dirname(abspath(__file__))))

# pylint: disable=wrong-import-position
from aioslimproto import SlimServer  # noqa: E402
from aioslimproto.const import EventType, SlimEvent  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
)

LOGGER = logging.getLogger("example")


async def main():
    """Run code example."""
    server = SlimServer()
    await server.start()

    # subscribe to events
    async def on_event(evt: SlimEvent):
        LOGGER.debug(f"Received event {evt.type.value} from player {evt.player_id}: {evt.data}")

    server.subscribe(on_event)

    # wait a bit for some players to discover the server and connect
    await asyncio.sleep(10)
    # send play request to a player names test
    for player in server.players:
        if player.name != "Sonos-542A1B59E814":
            continue
        await player.power(True)
        await player.volume_set(100)
        await player.play_url("http://icecast.omroep.nl/radio2-sb-mp3")

    await asyncio.sleep(3600)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
