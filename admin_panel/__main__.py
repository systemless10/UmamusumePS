
import argparse
import threading
import webbrowser

from .server import PanelServer


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the UmaPS admin panel.")
    ap.add_argument("--port", type=int, default=8189)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    ap.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                    help="extra Host header to accept (repeatable)")
    ap.add_argument("--setup", action="store_true",
                    help="start in setup mode: only the Setup window (paths, regions, game install)")
    ap.add_argument("--preload", action="store_true", help="start both region workers right away")
    args = ap.parse_args()
    try:
        server = PanelServer(args.port, args.allow_host)
    except OSError as e:
        raise SystemExit(f"Can't listen on port {args.port} ({e}); try --port") from e
    from . import settings as S
    eff = S.resolve()
    if args.setup or not eff["usable"]:
        server.pool.setup_mode = True
        print("Setup mode" + ("" if args.setup else ": no region has a usable master.mdb yet")
              + ". Finish it in the Setup window.")
    url = f"http://localhost:{server.port}"
    print(f"UmaPS Admin Panel at {url}  (Ctrl+C to stop)")
    if args.preload and not server.pool.setup_mode:
        for region in eff["usable"]:
            threading.Thread(target=server.pool.call, args=(region, "history.list", None, {"limit": 1}),
                             daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.jobs.close()
        server.pool.close()
        server.server_close()


if __name__ == "__main__":
    main()
