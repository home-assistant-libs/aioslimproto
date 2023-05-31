"""Logic for discovering slimproto clients on the local network."""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
from collections import OrderedDict
from typing import Optional

from .util import get_hostname, get_ip

LOGGER = logging.getLogger(__name__)

# pylint:disable=consider-using-f-string


async def start_discovery(
    control_port: int, cli_port: Optional[int], cli_port_json: Optional[int]
) -> asyncio.BaseTransport:
    """Start discovery for players."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DiscoveryProtocol(control_port, cli_port, cli_port_json),
        local_addr=("0.0.0.0", control_port),
    )
    return transport


class Datagram:
    """Description of a discovery datagram."""

    @classmethod
    def decode(cls, data):
        """Decode a datagram message."""
        data = data.decode(errors="replace")
        if data[0] == "e":
            return TLVDiscoveryRequestDatagram(data)
        if data[0] == "E":
            return TLVDiscoveryResponseDatagram(data)
        if data[0] == "d":
            return ClientDiscoveryDatagram(data)
        if data[0] == "h":
            pass  # Hello!
        if data[0] == "i":
            pass  # IR
        if data[0] == "2":
            pass  # i2c?
        if data[0] == "a":
            pass  # ack!


class ClientDiscoveryDatagram(Datagram):
    """Description of a client discovery datagram."""

    device = None
    firmware = None
    client = None

    def __init__(self, data):
        """Initialize class."""
        msg = struct.unpack("!cxBB8x6B", data.encode())
        self.device = msg[1]
        self.firmware = hex(msg[2])
        self.client = ":".join(["%02x" % (x,) for x in msg[3:]])

    def __repr__(self):
        """Print the class contents."""
        return "<%s device=%r firmware=%r client=%r>" % (
            self.__class__.__name__,
            self.device,
            self.firmware,
            self.client,
        )


class DiscoveryResponseDatagram(Datagram):
    """Description of a discovery response datagram."""

    def __init__(self, hostname, port):
        """Initialize class."""
        # pylint: disable=unused-argument
        hostname = hostname[:16].encode("UTF-8")
        hostname += (16 - len(hostname)) * "\x00"
        self.packet = struct.pack("!c16s", "D", hostname).decode()


class TLVDiscoveryRequestDatagram(Datagram):
    """Description of a discovery request datagram."""

    def __init__(self, data):
        """Initialize class."""
        requestdata = OrderedDict()
        idx = 1
        length = len(data) - 5
        while idx <= length:
            typ, _len = struct.unpack_from("4sB", data.encode(), idx)
            if _len:
                val = data[idx + 5 : idx + 5 + _len]
                idx += 5 + _len
            else:
                val = None
                idx += 5
            typ = typ.decode()
            requestdata[typ] = val
        self.data = requestdata

    def __repr__(self):
        """Pretty print class."""
        return "<%s data=%r>" % (self.__class__.__name__, self.data.items())


class TLVDiscoveryResponseDatagram(Datagram):
    """Description of a TLV discovery response datagram."""

    def __init__(self, responsedata):
        """Initialize class."""
        parts = ["E"]  # new discovery format
        for typ, value in responsedata.items():
            if value is None:
                value = ""
            elif len(value) > 255:
                # Response too long, truncating to 255 bytes
                value = value[:255]
            parts.extend((typ, chr(len(value)), value))
        self.packet = "".join(parts)


class DiscoveryProtocol:
    """Description of a discovery protocol."""

    def __init__(
        self,
        control_port: int,
        cli_port: Optional[int],
        cli_port_json: Optional[int],
    ):
        """Initialze class."""
        self.control_port = control_port
        self.cli_port = cli_port
        self.cli_port_json = cli_port_json
        self.transport = None

    def connection_made(self, transport):
        """Call on connection."""
        self.transport = transport
        # Allow receiving multicast broadcasts
        sock = self.transport.get_extra_info("socket")
        group = socket.inet_aton("239.255.255.250")
        mreq = struct.pack("4sL", group, socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    @classmethod
    def error_received(cls, exc):
        """Call on Error."""
        LOGGER.error(exc)

    @classmethod
    def connection_lost(cls, *args, **kwargs):
        """Call on Connection lost."""
        # pylint: disable=unused-argument
        LOGGER.debug("Connection lost to discovery")

    def build_tlv_response(self, requestdata):
        """Build TLV Response message."""
        responsedata = OrderedDict()
        for typ, value in requestdata.items():
            if typ == "NAME":
                # send full host name - no truncation
                value = get_hostname()
            elif typ == "IPAD":
                value = get_ip()
                # :todo: IPv6
                if value == "0.0.0.0":
                    # do not send back an ip address
                    typ = None
            elif typ == "JSON" and self.cli_port_json is not None:
                # send port as a string
                value = str(self.cli_port_json)
            elif typ == "CLIP" and self.cli_port is not None:
                # send port as a string
                value = str(self.cli_port)
            elif typ == "VERS":
                # send server version
                value = "7.9"
            elif typ == "UUID":
                # send server uuid
                value = "aioslimproto"
            elif typ == "JVID":
                # send server JVID
                value = "aioslimproto"
            else:
                LOGGER.debug("Unexpected information request: %r", typ)
                typ = None
            if typ:
                responsedata[typ] = value
        return responsedata

    def datagram_received(self, data, addr):
        """Datagram received callback."""
        # pylint: disable=broad-except
        try:
            dgram = Datagram.decode(data)
            if isinstance(dgram, ClientDiscoveryDatagram):
                self.send_discovery_response(addr)
            elif isinstance(dgram, TLVDiscoveryRequestDatagram):
                resonsedata = self.build_tlv_response(dgram.data)
                self.send_tlv_discovery_response(resonsedata, addr)
        except Exception:
            LOGGER.exception(
                "Error occured while trying to parse a datagram from %s - data: %s",
                addr,
                data,
            )

    def send_discovery_response(self, addr):
        """Send discovery response message."""
        dgram = DiscoveryResponseDatagram(get_hostname(), self.control_port)
        self.transport.sendto(dgram.packet.encode(), addr)

    def send_tlv_discovery_response(self, resonsedata, addr):
        """Send TLV discovery response message."""
        dgram = TLVDiscoveryResponseDatagram(resonsedata)
        self.transport.sendto(dgram.packet.encode(), addr)
