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
    # Always bind loopback only.
    uvicorn.run("gateway.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
