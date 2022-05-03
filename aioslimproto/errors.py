"""Exceptions for SlimProto."""


class SlimProtoException(Exception):
    """Base exception for errors."""


class UnsupportedContentType(SlimProtoException):
    """Raised when the contenttype can't be played by the player."""

class UnsupportedCommand(SlimProtoException):
    """Raised when a unsupported command is received on the CLI."""

class InvalidPlayer(SlimProtoException):
    """Raised when player was not found."""