"""
Manage the visualisation of a SlimProto based player.

Copyright 2010 Doug Winter
https://github.com/winjer/squeal/blob/master/src/squeal/player/display.py
"""
from __future__ import annotations

import struct

# pylint: disable=missing-class-docstring,invalid-name


class NoVisualisation:
    """Representation of a NoVisualisation visualisation."""

    which = 0

    def pack(self):
        """Pack data for sending."""
        return struct.pack("!BB", 0, 0)


class SpectrumChannel:
    """Model for a SpectrumChannel."""

    class Orientation:
        """Model for SpectrumChannel Orientation."""

        ltr = 0
        rtl = 1

    class Clipping:
        """Model for SpectrumChannel Clipping."""

        show_all = 0
        clip_higher = 1

    def __init__(
        self,
        position,
        width,
        orientation=Orientation.ltr,
        bar_width=4,
        bar_space=1,
        bar_grey=1,
        cap_grey=3,
        clipping=Clipping.show_all,
    ):
        """Init."""
        self.position = position
        self.width = width
        self.orientation = orientation
        self.bar_width = bar_width
        self.bar_space = bar_space
        self.bar_grey = bar_grey
        self.cap_grey = cap_grey
        self.clipping = clipping

    def pack(self):
        """Pack data for sending."""
        return struct.pack(
            "!8I",
            self.position,
            self.width,
            self.orientation,
            self.bar_width,
            self.bar_space,
            self.clipping,
            self.bar_grey,
            self.cap_grey,
        )


class SpectrumAnalyser:
    """Representation of a SpectrumAnalyser visualisation."""

    which = 2

    class Channel:
        """Model for SpectrumAnalyser Channel."""

        stereo = 0
        mono = 1

    class Bandwidth:
        """Model for SpectrumAnalyser Bandwidth."""

        high = 0
        low = 1

    def __init__(
        self,
        channels=Channel.stereo,
        bandwidth=Bandwidth.high,
        preemphasis=0x10000,
        left=SpectrumChannel(position=0, width=160),
        right=SpectrumChannel(position=160, width=160),
    ):
        """Init."""
        self.channels = channels
        self.bandwidth = bandwidth
        self.preemphasis = preemphasis
        self.left = left
        self.right = right

    def pack(self):
        """Pack data for sending."""
        if self.channels == self.Channel.stereo:
            count = 3 + 8 + 8
        else:
            count = 3
        header = struct.pack("!BB", self.which, count)
        basic = struct.pack("!III", self.channels, self.bandwidth, self.preemphasis)
        left = self.left.pack()
        if self.channels == self.Channel.stereo:
            right = self.right.pack()
        else:
            right = ""
        return header + basic + left + right


class VUMeterChannel:
    """Model for a VUMeterChannel."""

    def __init__(self, position, width):
        """Init."""
        self.position = position
        self.width = width


class VUMeter:
    """Representation of a VUMeter visualisation."""

    which = 1

    class Style:
        """Model for a VUMeter Style."""

        digital = 0
        analog = 1

    class Channel:
        """Model for a VUMeter Channel."""

        stereo = 0
        mono = 1

    def __init__(
        self,
        style=Style.digital,
        channel=Channel.stereo,
        left=VUMeterChannel(0, 160),
        right=VUMeterChannel(160, 160),
    ):
        """Init."""
        self.style = style
        self.channel = channel
        self.left = left
        self.right = right


VisualisationType = NoVisualisation | SpectrumAnalyser | VUMeter
