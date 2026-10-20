import argparse
import os

import uvicorn

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
    uvicorn.run("gateway.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
