"""Tests for CLI."""

from aioslimproto import cli


def test_msg_instantiation():
    """Test instantiating a CLI message."""
    raw_message = "aa:b:cc:dd:ee mixer volume 50"
    msg = cli.CLIMessage.from_string(raw_message)
    assert msg.player_id == "aa:b:cc:dd:ee"
    assert msg.command_str == "mixer volume 50"
    assert msg.command == "mixer"
    assert msg.command_args == ["volume", "50"]

    # No command args
    raw_message = "aa:b:cc:dd:ee pause"
    msg = cli.CLIMessage.from_string(raw_message)
    assert msg.player_id == "aa:b:cc:dd:ee"
    assert msg.command_str == "pause"
    assert msg.command == "pause"
    assert msg.command_args == []


def test_json_msg_instantiation():
    """Test instantiating a JSON RPC message."""
    msg = cli.JSONRPCMessage.from_json(
        id=1, method="method", params=["aa:b:cc:dd:ee", ["mixer", "volume", 50]]
    )
    assert msg.id == 1
    assert msg.method == "method"
    assert msg.player_id == "aa:b:cc:dd:ee"
    assert msg.command == "mixer"
    assert msg.command_args == ["volume", "50"]
    assert msg.command_str == "mixer volume 50"

    # No command args
    msg = cli.JSONRPCMessage.from_json(
        id=1, method="method", params=["aa:b:cc:dd:ee", ["pause"]]
    )
    assert msg.id == 1
    assert msg.method == "method"
    assert msg.player_id == "aa:b:cc:dd:ee"
    assert msg.command == "pause"
    assert msg.command_args == []
    assert msg.command_str == "pause"
