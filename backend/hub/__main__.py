"""
python -m hub            (run from the backend/ folder; start-hub.ps1 does this for you)

Starts the Moonwalk Hub on 127.0.0.1 and opens the website with your private token in the
URL *fragment* (#token=…) — fragments are never sent to servers or written to access logs.
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def main() -> None:
    parser = argparse.ArgumentParser(description="ClarityOS Hub")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOONWALK_HUB_PORT", "8787")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("!" * 70)
        print(f"WARNING: binding to {args.host} exposes control of this computer to your network.")
        print("Only do this on a network you trust, and put TLS in front of it.")
        print("!" * 70)

    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BACKEND, ".env"))

    import uvicorn
    from hub import config as cfg
    from hub.app import create_app

    tokens = cfg.load_tokens()
    url = f"http://127.0.0.1:{args.port}/#token={tokens['ui']}"
    print(f"\nClarityOS Hub {cfg.VERSION}")
    print(f"  Website : http://127.0.0.1:{args.port}/")
    print(f"  Data dir: {cfg.user_dir()}")
    print("  Login   : the browser opens with your token; if not, open the line below")
    print(f"            {url}\n")
    if not args.no_open:
        webbrowser.open(url)
    uvicorn.run(create_app(port=args.port), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
