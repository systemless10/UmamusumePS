
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import sys
import threading

from . import region, state

log = logging.getLogger("uma-server")


@asynccontextmanager
async def lifespan(app):
    try:
        async with _admin_panel(app):
            yield
    finally:
        state.close()
        log.info("State saved and closed.")


@asynccontextmanager
async def _admin_panel(app):
    if os.environ.get("UMA_ADMIN_ENABLED", "1").strip().lower() in {"0", "false", "off"}:
        yield
        return
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from admin_panel.server import PanelServer

    port = int(os.environ.get("UMA_ADMIN_PORT", "8190" if region.PROCESS_REGION == "jp" else "8189"))
    if not 0 <= port <= 65535:
        raise ValueError("UMA_ADMIN_PORT must be between 0 and 65535")
    try:
        hosts = [h.strip() for h in os.environ.get("UMA_ADMIN_HOSTS", "").split(",") if h.strip()]
        panel = PanelServer(port, extra_hosts=hosts)
    except OSError as ex:
        raise RuntimeError(
            f"Cannot start admin panel on localhost:{port}. Stop the standalone panel, "
            "set UMA_ADMIN_PORT to a free port, or set UMA_ADMIN_ENABLED=0."
        ) from ex
    thread = threading.Thread(target=panel.serve_forever, name="uma-admin", daemon=True)
    app.state.admin_panel = panel
    thread.start()
    log.info("Admin panel: http://localhost:%s (%s PS)", panel.port, region.PROCESS_REGION)
    try:
        yield
    finally:
        panel.shutdown()
        thread.join()
        panel.pool.close()
        panel.server_close()
        app.state.admin_panel = None
