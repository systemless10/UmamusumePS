
import os
from pathlib import Path
import runpy
import sys
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin_panel import settings as S
from admin_panel.server import PanelServer


def prepare() -> None:
    region = os.environ.get("UMA_REGION", "global")
    if region in S.resolve()["usable"]:
        return
    port = int(os.environ.get("UMA_ADMIN_PORT", "8190" if region == "jp" else "8189"))
    hosts = [h.strip() for h in os.environ.get("UMA_ADMIN_HOSTS", "").split(",") if h.strip()]
    panel = PanelServer(port, extra_hosts=hosts)
    panel.required_region = region
    panel.pool.setup_mode = True
    panel.timeout = 0.25
    url = f"http://localhost:{panel.port}"
    print(f"No usable {region.upper()} master.mdb. Open {url}, select your master.mdb "
          "and game folder in Setup, then click Finish setup to start the server.", flush=True)
    try:
        webbrowser.open(url)
        while panel.pool.setup_mode:
            panel.handle_request()
    finally:
        panel.jobs.close()
        panel.pool.close()
        panel.server_close()


def main() -> None:
    if sys.argv[1:] == ["--prepare"]:
        prepare()
        return
    region = os.environ.get("UMA_REGION", "global")
    eff = S.apply()
    if region not in eff["usable"]:
        raise SystemExit(f"No usable {region.upper()} master.mdb. Run the server launcher to finish Setup.")
    os.environ["MASTER_MDB_PATH"] = eff["regions"][region]["master"]
    sys.argv = ["uvicorn", *sys.argv[1:]]
    runpy.run_module("uvicorn", run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
