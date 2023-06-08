"""
Manage the display of a SlimProto based player.

Provides support for rendering text using local truetype fonts.

Copyright 2010 Doug Winter
https://github.com/winjer/squeal/blob/master/src/squeal/player/display.py
"""
import os
import struct

from PIL import Image, ImageDraw, ImageFont

# pylint: disable=missing-class-docstring,invalid-name

fontdir = os.path.join(os.path.dirname(__file__), "font")


class Font:
    """Respresentation of a font for Slimproto displays."""

    def __init__(self, name):  # noqa
        self.filename = os.path.join(fontdir, name + ".ttf")
        self.cache = {}

    def render(self, s, size=15):
        """Return a PIL image with this string rendered into it."""
        self.cache.setdefault(size, ImageFont.truetype(self.filename, size))
        font = self.cache[size]
        siz = font.getsize(s)
        im = Image.new("RGB", siz)
        draw = ImageDraw.Draw(im)
        draw.text((0, 0), s, font=font)
        return im


class SlimProtoDisplay:
    """Reresentation of a Slimproto display."""

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

    def __init__(self):
        """Initialize."""
        self.image = Image.new("1", (320, 32))
        self.fonts = {}
        for f in self.availableFonts():
            self.fonts[f] = Font(f)

    def clear(self):
        """Clear the display."""
        self.image.paste(0, (0, 0, 320, 32))

    def availableFonts(self):
        """Return available fonts."""
        for f in os.listdir(fontdir):
            if f.endswith(".ttf"):
                yield f[: -len(".ttf")]

    def renderText(self, text, fontName, size, position):
        """Render given text on display."""
        font = self.fonts[fontName]
        im = font.render(text, size)
        self.image.paste(im, position)

    def frame(self):
        """Return the frame ready for transmission."""
        pixmap = self.image.load()
        words = []
        for col in range(0, 320):
            word = 0
            for bit in range(0, 32):
                to_set = 1 if pixmap[col, 31 - bit] else 0
                word += to_set << bit
            words.append(word)
        return struct.pack("!320I", *words)
