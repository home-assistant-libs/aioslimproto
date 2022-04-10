"""Exceptions for SlimProto."""


class SlimProtoException(Exception):
    """Base exception for errors."""


class UnsupportedContentType(Exception):
    """Raised when the contenttype can't be played by the player."""
