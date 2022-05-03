"""Tests for JSON RPC."""

from aioslimproto import json_rpc


def test_msg_instantiation():
    """Test instantiating a JSON RPC message."""
    msg = json_rpc.JSONRPCMessage.from_json(
        id=1, method="method", params=[3, ["command", 5]]
    )
    assert msg.id == 1
    assert msg.method == "method"
    assert msg.player_id == "3"
    assert msg.command_args == ["5"]
    assert msg.command_str == "command 5"

    # No command args
    msg = json_rpc.JSONRPCMessage.from_json(
        id=1, method="method", params=[3, ["command"]]
    )
    assert msg.id == 1
    assert msg.method == "method"
    assert msg.player_id == "3"
    assert msg.command_args == []
    assert msg.command_str == "command"
