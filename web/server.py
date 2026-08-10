from __future__ import annotations

import argparse
import functools
import http.server
import os
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="SursumAI web static server")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    os.chdir(WEB_DIR)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    with http.server.ThreadingHTTPServer((args.host, args.port), handler) as httpd:
        print(f"SursumAI web on http://{args.host}:{args.port}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
