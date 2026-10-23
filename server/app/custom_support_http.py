from fastapi import APIRouter, HTTPException, Request, Response

from . import custom_support as C

router = APIRouter(prefix="/custom-support")


@router.post("/bootstrap")
async def bootstrap(request: Request):
    try:
        p = await request.json()
        if request.headers.get("x-uma-custom-plugin") != "1":
            raise ValueError("A compatible custom support plugin is required")
        return C.bootstrap(p.get("protocol"), p.get("region"), p.get("base_sha256"))
    except (ValueError, AttributeError) as e:
        raise HTTPException(409, str(e)) from e


@router.get("/pack/{challenge}/{kind}")
async def pack(challenge: str, kind: str):
    import time
    with C._lock:
        ch = C._challenges.get(challenge)
        if not ch or ch["expires"] < time.time() or not C.is_enabled():
            raise HTTPException(403, "Plugin bootstrap required")
        m = C.manifest(ch["digest"])
        if not m or ch["region"] != C.region.CURRENT_REGION.get():
            raise HTTPException(403, "Wrong region")
        if kind == "master.mdb":
            filename, media = kind, "application/vnd.sqlite3"
        else:
            entries = {str(d["id"]) + ".png" for d in m["cards"] if d["art_sha256"]}
            entries |= {f'{d["id"]}_{slot}.png' for d in m["cards"] for slot in d.get("slot_art") or {}}
            if kind not in entries:
                raise HTTPException(404, "No such artwork")
            filename, media = kind, "image/png"
        return Response((C.root() / "packs" / ch["digest"] / filename).read_bytes(),
                        media_type=media, headers={"Cache-Control": "no-store"})


@router.post("/ready")
async def ready(request: Request):
    try:
        p = await request.json()
        return C.ready(p.get("challenge"), p.get("digest"), p.get("master_sha256"), p.get("instance"))
    except (ValueError, AttributeError) as e:
        raise HTTPException(409, str(e)) from e


@router.post("/renew")
async def renew(request: Request):
    try:
        p = await request.json()
        return C.renew(p.get("token"))
    except (ValueError, AttributeError) as e:
        raise HTTPException(409, str(e)) from e
