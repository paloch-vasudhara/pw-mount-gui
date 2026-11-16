import argparse
import logging
import os

import uvicorn

from .logsetup import configure_logging
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
    path = configure_logging()
    logging.getLogger("analysis").info("analysis starting on 127.0.0.1:%d, log file %s",
                                       args.port, path)
    # Always bind loopback only.
    uvicorn.run("analysis.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
