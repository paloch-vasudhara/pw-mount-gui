import argparse
import os

import uvicorn

from .config import WS_MAX_MESSAGE_BYTES, WS_PING_INTERVAL_S, WS_PING_TIMEOUT_S

DEFAULT_PORT = 8400


def main() -> None:
    parser = argparse.ArgumentParser(prog="gateway")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PW_GATEWAY_PORT", DEFAULT_PORT)),
    )
    args = parser.parse_args()
    # Host/Origin allow-lists derive from the port: set it before the app module loads.
    os.environ["PW_GATEWAY_PORT"] = str(args.port)
    # Always bind loopback only.
    # Protocol-level ping/pong (server-driven) is the link-loss detector (G4/I3).
    uvicorn.run(
        "gateway.app:app",
        host="127.0.0.1",
        port=args.port,
        ws_ping_interval=WS_PING_INTERVAL_S,
        ws_ping_timeout=WS_PING_TIMEOUT_S,
        ws_max_size=WS_MAX_MESSAGE_BYTES,
    )


if __name__ == "__main__":
    main()
