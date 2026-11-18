"""Real CLI server wrapped only with fixture PID recording and network denial."""

import os
import runpy
import socket
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
original_connect = socket.socket.connect


def offline_connect(instance, address):
    if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
        return original_connect(instance, address)
    raise AssertionError("offline MCP fixture cannot connect to a remote service")


socket.socket.connect = offline_connect
sys.argv = ["corecoder", "mcp", "serve-knowledge", *sys.argv[2:]]
runpy.run_module("corecoder", run_name="__main__")
