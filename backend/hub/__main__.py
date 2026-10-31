"""
python -m hub            (run from the backend/ folder; start-hub.ps1 does this for you)

Starts the ClarityOS Hub on 127.0.0.1 and opens the website with your private token in the
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


def _open_dashboard(url: str) -> None:
    """Open the dashboard in Chrome when possible.

    The hub's page-reading panels need the ClarityOS extension, which lives in
    Chrome — not in whatever Windows has set as the default browser.
    """
    try:
        from tools.browser_target import bridge_browser, launch_url, resolve_executable
        target = bridge_browser() or ("chrome" if resolve_executable("chrome") else "")
        if target:
            opened, _ = launch_url(target, url)
            if opened:
                return
    except Exception:
        pass
    webbrowser.open(url)


def main() -> None:
    parser = argparse.ArgumentParser(description="ClarityOS Hub")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CLARITYOS_HUB_PORT", "8787")))
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
    # A hub may already be serving this port — commonly a previous
    # start-hub.ps1 that is still running. Binding again raises WinError 10048
    # and uvicorn prints a raw traceback, which reads like the hub is broken.
    # It is not: the dashboard is up, this process is simply redundant.
    import socket
    def _port_busy(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.6)
            return probe.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0

    if _port_busy(args.host, args.port):
        print(f"  A ClarityOS Hub is already running on port {args.port}.")
        print("  Not starting a second one - open the link above to use it.")
        print("  To restart instead, close the other hub window first." + chr(10))
        if not args.no_open:
            _open_dashboard(url)
        return

    if not args.no_open:
        # Open in Chrome when it is available: the hub's page-reading panels
        # depend on the ClarityOS extension, which is installed in Chrome, not
        # in whatever Windows has set as the default browser (Edge here). The
        # old call handed the tokenised URL to the default browser, so the
        # dashboard came up in a browser with no extension — and anyone opening
        # it in Chrome by hand got "Wrong or missing token" instead.
        _open_dashboard(url)
    uvicorn.run(create_app(port=args.port), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
