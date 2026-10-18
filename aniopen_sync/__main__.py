import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    data_dir = Path(os.environ.get("ANIOPEN_DATA_DIR", "./data"))
    if len(sys.argv) > 1 and sys.argv[1] in ("token", "backup"):
        command = sys.argv[1]
        parser = argparse.ArgumentParser(prog="aniopen-sync " + command)
        parser.add_argument("--data-dir", type=Path, default=data_dir)
        if command == "backup":
            parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        if command == "token":
            from .security import access_token

            print(access_token(args.data_dir))
        else:
            from .backup import create_backup

            print(create_backup(args.data_dir, args.output))
        return
    parser = argparse.ArgumentParser(description="ANiOpen Sync")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    uvicorn.run("aniopen_sync.app:create_app", factory=True, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
