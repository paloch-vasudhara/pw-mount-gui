import argparse
import os

import uvicorn

from .priority import set_below_normal

DEFAULT_PORT = 8410


def main() -> None:
    parser = argparse.ArgumentParser(prog="analysis")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PW_ANALYSIS_PORT", DEFAULT_PORT)),
    )
    args = parser.parse_args()
    set_below_normal()
    # Always bind loopback only.
    uvicorn.run("analysis.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
