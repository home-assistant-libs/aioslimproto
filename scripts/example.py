"""aioslimproto example.."""  # noqa: INP001

import asyncio
import contextlib
import logging

from aioslimproto import SlimServer
from aioslimproto.models import EventType, SlimEvent

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)-15s %(levelname)-5s %(name)s -- %(message)s",
)

LOGGER = logging.getLogger("example")


async def main() -> None:
    """Run code example."""
    server = SlimServer()
    await server.start()

    # subscribe to events
    async def on_event(evt: SlimEvent) -> None:
        if evt.type == EventType.PLAYER_HEARTBEAT:
            return  # too spammy
        LOGGER.debug(
            "Received event %s from player %s: %s",
            evt.type.value,
            evt.player_id,
            evt.data,
        )
        if evt.type == EventType.PLAYER_CONNECTED:
            player = server.get_player(evt.player_id)
            # play some radio url after a while
            await asyncio.sleep(5)
            await player.power(powered=True)
            await player.volume_set(50)
            await player.play_url("http://icecast.omroep.nl/radio2-sb-mp3", "audio/mp3")

    server.subscribe(on_event)

    # wait a bit for some players to discover the server and connect
    await asyncio.sleep(3600)


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main())
