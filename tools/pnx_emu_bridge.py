#!/usr/bin/env python3
"""Persistent bridge to a running Pebble emulator's button input.

Exists because `pebble emu-button` is a fresh CLI subprocess per call, and its own
connection setup (pebble_tool.sdk.emulator.ManagedEmulatorTransport.connect) sleeps
0.5s before even ATTEMPTING to connect -- every single invocation, since the CLI has
no way to keep a connection open between commands. Measured at ~670-680ms per button
press through that path, consistently -- unusable for holding a button or navigating
a menu, which is what "Resonant runs terribly" turned out to actually be.

Run under pebble-tool's OWN Python, not the editor's -- libpebble2 lives in that venv
(see Emulator._bridge_interpreter in pnx_editor.py for how that interpreter is found,
by reading `pebble`'s own shebang rather than guessing a venv layout). Talks a plain
line-based protocol on stdin/stdout to the editor instead of pebble-tool's CLI:

    push up select      -> ok            (sets the FULL set of buttons held)
    release             -> ok            (nothing held)
    ping                -> ok
    (anything else)     -> err <message>
    (EOF on stdin)      -> exit(0)

Deliberately imports nothing from pebble_tool itself: only libpebble2, which is
enough to open the same websocket ManagedEmulatorTransport does
(ws://localhost:<pypkjs port>/) and send the same QemuButton packet `emu-button`
does -- just once, kept open, instead of reconnecting on its own 0.5s timer every
single press.
"""
import sys

from libpebble2.communication.transports.websocket import MessageTargetPhone, WebsocketTransport
from libpebble2.communication.transports.websocket.protocol import WebSocketRelayQemu
from libpebble2.communication.transports.qemu.protocol import QemuButton, QemuPacket

BUTTONS = {
    "back": QemuButton.Button.Back,
    "up": QemuButton.Button.Up,
    "select": QemuButton.Button.Select,
    "down": QemuButton.Button.Down,
}


def send_button(transport, state):
    data = QemuButton(state=state)
    packet = QemuPacket(data=data)
    packet.serialise()  # assigns packet.protocol from the union -- required before reading it
    transport.send_packet(WebSocketRelayQemu(protocol=packet.protocol, data=data.serialise()),
                          target=MessageTargetPhone())


def resolve_state(cmd, args):
    """cmd/args -> the QemuButton bitmask to send, or None for a no-op command."""
    if cmd == "push":
        if not args:
            raise ValueError("push needs at least one button")
        bad = [a for a in args if a not in BUTTONS]
        if bad:
            raise ValueError(f"unknown button(s): {', '.join(bad)}")
        state = 0
        for a in args:
            state |= BUTTONS[a]
        return state
    if cmd == "release":
        return 0
    if cmd == "ping":
        return None
    raise ValueError(f"unknown command: {cmd!r}")


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "--port":
        print("usage: pnx_emu_bridge.py --port <pypkjs-port>", file=sys.stderr)
        return 2
    port = sys.argv[2]

    transport = WebsocketTransport(f"ws://localhost:{port}/")
    try:
        transport.connect()
    except Exception as e:                                       # noqa: BLE001
        print(f"connect failed: {e}", file=sys.stderr)
        return 1

    # The one line the parent waits for before treating this process as usable --
    # see Emulator._spawn_bridge, pnx_editor.py.
    print("ready", flush=True)

    for line in sys.stdin:
        parts = line.split()
        if not parts:
            continue
        cmd, args = parts[0], parts[1:]
        try:
            state = resolve_state(cmd, args)
        except ValueError as e:
            print(f"err {e}", flush=True)
            continue

        if state is None:                                        # ping
            print("ok", flush=True)
            continue

        try:
            send_button(transport, state)
            print("ok", flush=True)
        except Exception:                                         # noqa: BLE001
            # The one thing here that can legitimately go stale over a long-lived
            # bridge's life: the emulator side, not this connection's own code. One
            # reconnect attempt before reporting failure -- if pypkjs itself is gone,
            # this also fails and the parent will tear the bridge down and retry
            # fresh on the next press (Emulator._bridge_send, pnx_editor.py).
            try:
                transport.connect()
                send_button(transport, state)
                print("ok", flush=True)
            except Exception as e2:                                # noqa: BLE001
                print(f"err {e2}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
