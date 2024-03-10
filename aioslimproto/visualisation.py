"""
Manage the visualisation of a SlimProto based player.

Copyright 2010 Doug Winter
https://github.com/winjer/squeal/blob/master/src/squeal/player/display.py
"""

from __future__ import annotations

import struct


class NoVisualisation:
    """Representation of a disabled visualisation."""

    id = 0

    def pack(self) -> None:
        """Pack data for sending."""
        return struct.pack("!BB", self.id, 0)


class WaveForm:
    """Representation of a WaveForm visualisation."""

    id = 3

    def pack(self) -> None:
        """Pack data for sending."""
        return struct.pack("!BB", self.id, 0)


class SpectrumAnalyser:
    """Representation of a SpectrumAnalyser visualisation."""

    id = 2

    class Channels:
        """Model for SpectrumAnalyser Channel."""

        stereo = 0
        mono = 1

    class Bandwidth:
        """Model for SpectrumAnalyser Bandwidth."""

        high = 0
        low = 1

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
            position: int,
            width: int,
            orientation: Orientation = Orientation.ltr,
            bar_width: int = 4,
            bar_space: int = 1,
            bar_grey: int = 1,
            cap_grey: int = 3,
            clipping: int = Clipping.clip_higher,
        ) -> None:
            """Init."""
            self.position = position
            self.width = width
            self.orientation = orientation
            self.bar_width = bar_width
            self.bar_space = bar_space
            self.bar_grey = bar_grey
            self.cap_grey = cap_grey
            self.clipping = clipping

        def pack(self) -> None:
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

    def __init__(
        self,
        channels: int = Channels.stereo,
        bandwidth: int = Bandwidth.high,
        preemphasis: int = 0x10000,
        left: SpectrumChannel = SpectrumChannel(position=0, width=160),  # noqa: B008
        right: SpectrumChannel = SpectrumChannel(position=160, width=160),  # noqa: B008
    ) -> None:
        """Init."""
        self.channels = channels
        self.bandwidth = bandwidth
        self.preemphasis = preemphasis
        self.left = left
        self.right = right

    def pack(self) -> None:
        """Pack data for sending."""
        # Parameters for the spectrum analyzer:
        #   0 - Channels: stereo == 0, mono == 1
        #   1 - Bandwidth: 0..22050Hz == 0, 0..11025Hz == 1
        #   2 - Preemphasis in dB per KHz
        # Left channel parameters:
        #   3 - Position in pixels
        #   4 - Width in pixels
        #   5 - orientation: left to right == 0, right to left == 1
        #   6 - Bar width in pixels
        #   7 - Bar spacing in pixels
        #   8 - Clipping: show all subbands == 0, clip higher subbands == 1
        #   9 - Bar intensity (greyscale): 1-3
        #   10 - Bar cap intensity (greyscale): 1-3
        # Right channel parameters (not required for mono):
        #   11-18 - same as left channel parameters
        count = 3 + 8 + 8 if self.channels == self.Channels.stereo else 3
        header = struct.pack("!BB", self.id, count)
        basic = struct.pack("!III", self.channels, self.bandwidth, self.preemphasis)
        left = self.left.pack()
        right = self.right.pack() if self.channels == self.Channels.stereo else ""
        return header + basic + left + right


class VUMeter:
    """Representation of a VUMeter visualisation."""

    id = 1

    class Style:
        """VUMeter Style."""

        digital = 0
        analog = 1

    class Channels:
        """VUMeter Channels."""

        stereo = 0
        mono = 1

    class VUMeterChannel:
        """VUMeterChannel."""

        def __init__(self, position: int, width: int) -> None:
            """Init."""
            self.position = position
            self.width = width

        def pack(self) -> None:
            """Pack data for sending."""
            return struct.pack(
                "!2I",
                self.position,
                self.width,
            )

    def __init__(
        self,
        style: int = Style.analog,
        channels: int = Channels.stereo,
        left: VUMeterChannel = VUMeterChannel(0, 160),  # noqa: B008
        right: VUMeterChannel = VUMeterChannel(160, 160),  # noqa: B008
    ) -> None:
        """Init."""
        self.style = style
        self.channels = channels
        self.left = left
        self.right = right

    def pack(self) -> None:
        """Pack data for sending."""
        # Parameters for the vumeter:
        #   0 - Channels: stereo == 0, mono == 1
        #   1 - Style: digital == 0, analog == 1
        # Left channel parameters:
        #   2 - Position in pixels
        #   3 - Width in pixels
        # Right channel parameters (not required for mono):
        #   4-5 - same as left channel parameters
        header = struct.pack("!BBB", self.id, self.channels, self.style)

        count = 3 + 8 + 8 if self.channels == self.Channels.stereo else 3
        header = struct.pack("!BB", self.id, count)
        basic = struct.pack("!II", self.channels, self.style)

        left = self.left.pack()
        right = self.right.pack() if self.channels == self.Channels.stereo else b""
        return header + basic + left + right


class VUMeterESP32:
    """Representation of a VUMeterESP32 visualisation."""

    id = 0x11

    class Style:
        """Model for a VUMeter Style."""

        digital = 0
        analog = 1

    def __init__(
        self,
        style: int = Style.analog,
        width: int = 128,
    ) -> None:
        """Init."""
        self.style = style
        self.width = width

    def pack(self) -> None:
        """Pack data for sending."""
        return struct.pack("!BBII", self.id, 2, self.width, self.style)


class SpectrumAnalyserESP32:
    """Representation of a SpectrumAnalyserESP32 visualisation."""

    id = 0x12

    def __init__(self, width: int = 128) -> None:
        """Init."""
        self.width = width

    def pack(self) -> None:
        """Pack data for sending."""
        return struct.pack("!BBIII", self.id, 3, self.width, 8, 25)
