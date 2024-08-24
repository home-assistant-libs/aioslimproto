"""
Manage the display of a SlimProto based player.

Provides support for rendering text using local truetype fonts.

Copyright 2010 Doug Winter
https://github.com/winjer/squeal/blob/master/src/squeal/player/display.py
"""

from __future__ import annotations

import asyncio
import os
import struct
import logging
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

from .models import VisualisationType
from .visualisation import (
    NoVisualisation,
    SpectrumAnalyser,
    SpectrumAnalyserESP32,
    VUMeter,
    VUMeterESP32,
    WaveForm,
)

if TYPE_CHECKING:
    from .client import SlimClient

FONTDIR = os.path.join(os.path.dirname(__file__), "font")  # noqa: PTH120, PTH118
AVAILABLE_FONTS = [f[: -len(".ttf")] for f in os.listdir(FONTDIR) if f.endswith(".ttf")]

# ruff: noqa: FBT001,FBT002


class Font:
    """Representation of a font for Slimproto displays."""

    def __init__(self, name: str) -> None:
        """Initiatlzie Font."""
        self.filename = os.path.join(FONTDIR, name + ".ttf")  # noqa: PTH118
        self.cache: dict[int, ImageFont.FreeTypeFont] = {}

    def render(
        self,
        text: str,
        size: int = 15,
        linewidth: int = 320,
        lineheight: int = 0,
    ) -> Image:
        """Return a PIL image with this string rendered into it."""
        if size not in self.cache:
            self.cache[size] = ImageFont.truetype(self.filename, size)
        font: ImageFont.FreeTypeFont = self.cache[size]
        bbox = font.getbbox(text)
        height = lineheight or abs(bbox[1] - bbox[3]) + 2
        im = Image.new("1", (linewidth, height))
        draw = ImageDraw.Draw(im)
        draw.text((0, 0), text, font=font, fill=255)
        return im


class SlimProtoDisplay:
    """Representation of a Slimproto display."""

    disabled: bool = False
    height: int = 32
    last_brightness: int = -1
    last_line_one: str = ""
    last_line_two: str = ""
    last_fullscreen_line: str = ""

    class Transition:
        """Model for a Transition."""

        clear = "c"
        push_left = "r"
        push_right = "l"
        push_up = "u"
        push_down = "d"
        bump_left = "L"
        bump_right = "R"
        bump_down = "U"
        bump_up = "D"

    class AnimateState:
        """Model for a AnimateState."""

        none = 0
        client = 1
        scheduled = 2
        server_brief = 5
        clear_scroll = 6

    class ScrollState:
        """Model for a ScrollState."""

        none = 0
        server_normal = 1
        server_ticker = 2

    def __init__(
        self,
        player: SlimClient,
        width: int = 128,
        visualisation_type: VisualisationType = VisualisationType.SPECTRUM_ANALYZER,
    ) -> None:
        """Initialize."""
        self.logger = logging.getLogger(__name__)
        self.player = player
        self.width = width
        self.visualisation_type = visualisation_type
        # NOTE: height can only be 32, its not adjustable
        self.image = Image.new("1", (self.width, 32))
        self.fonts: dict[str, Font] = {}
        for f in AVAILABLE_FONTS:
            self.fonts[f] = Font(f)

    async def clear(self) -> None:
        """Clear the display."""
        self.image.paste(0, (0, 0, self.width, 32))
        await self._render_display()

    async def set_lines(
        self,
        line_one: str = "",
        line_two: str = "",
        fullscreen: str = "",
    ) -> None:
        """Set (predefined) text lines on display."""
        if fullscreen:
            # clear regular lines when setting fullscreen
            self.last_line_one = ""
            self.last_line_two = ""
        else:
            self.last_fullscreen_line = ""
        # process line 1
        if self.last_line_one != line_one:
            self.last_line_one = line_one
            font = self.fonts["DejaVu-Sans"]
            im = font.render(line_one, 11, self.width, 14)
            self.image.paste(im, (0, 0))
        # process line 2
        if self.last_line_two != line_two:
            self.last_line_two = line_two
            font = self.fonts["DejaVu-Sans-Condensed-Bold"]
            if len(line_two) > self.width / 8:
                font_size = 12
            elif len(line_two) > self.width / 4:
                font_size = 14
            else:
                font_size = 16
            im = font.render(line_two, font_size, self.width, 18)
            self.image.paste(im, (0, 14))
        # process fullscreen message/text
        if self.last_fullscreen_line != fullscreen:
            self.last_fullscreen_line = fullscreen
            font = self.fonts["DejaVu-Sans-Condensed-Bold"]
            im = font.render(fullscreen, 22, self.width, 32)
            self.image.paste(im, (0, 0))
        await self._render_display()

    async def _render_display(
        self,
        transition: str = Transition.clear,
        offset: int = 0,
        param: int = 0,
    ) -> None:
        """Render display and send it to the player."""

        def pack() -> bytes:
            """Return the packed frame ready for transmission."""
            if self.width != self.image.width:
                self.logger.warn("Attempted to render %s x %s Image to %s x %s Display", self.image.width, self.image.height, self.width, self.height)
            pixmap = self.image.load()
            words = []
            for col in range(self.width):
                word = 0
                for bit in range(32):
                    to_set = 1 if pixmap[col, 31 - bit] else 0
                    word += to_set << bit
                words.append(word)
            bitmap = struct.pack(f"!{self.width}I", *words)
            return struct.pack("!Hcb", offset, transition.encode(), param) + bitmap

        data = await asyncio.get_running_loop().run_in_executor(None, pack)
        await self.player.send_frame(b"grfe", data)

    async def set_brightness(self, brightness: int) -> None:
        """Set brightness of display."""
        brightness = max(brightness, 0)
        brightness = min(brightness, 4)
        if self.last_brightness == brightness:
            return
        await self.player.send_frame(b"grfb", struct.pack("!H", brightness))
        self.last_brightness = brightness

    async def set_visualization(self, show: bool = True) -> None:
        """Show visualization."""
        esp32 = self.player.device_type == "squeezeesp32"
        if show:
            if self.visualisation_type == VisualisationType.SPECTRUM_ANALYZER and esp32:
                data = SpectrumAnalyserESP32(width=self.width).pack()
            elif self.visualisation_type == VisualisationType.VU_METER_ANALOG and esp32:
                data = VUMeterESP32(VUMeter.Style.analog, self.width).pack()
            elif (
                self.visualisation_type == VisualisationType.VU_METER_DIGITAL and esp32
            ):
                data = VUMeterESP32(VUMeter.Style.digital, self.width).pack()
            elif self.visualisation_type == VisualisationType.SPECTRUM_ANALYZER:
                data = SpectrumAnalyser().pack()
            elif self.visualisation_type == VisualisationType.VU_METER_ANALOG:
                data = VUMeter(VUMeter.Style.analog).pack()
            elif self.visualisation_type == VisualisationType.VU_METER_DIGITAL:
                data = VUMeter(VUMeter.Style.digital).pack()
            elif self.visualisation_type == VisualisationType.WAVEFORM:
                data = WaveForm().pack()
            else:
                data = NoVisualisation().pack()
            await self.player.send_frame(b"visu", data)
        else:
            # disable visualization
            await self.player.send_frame(b"visu", NoVisualisation().pack())
