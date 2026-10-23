use super::*;
use serde_json::{json, Value};
use std::collections::HashMap;

pub static ENABLED: AtomicBool = AtomicBool::new(false);
pub static SERVER_EVENTS: AtomicBool = AtomicBool::new(false);
pub static UNRELEASED: AtomicBool = AtomicBool::new(false);
pub fn enabled() -> bool {
    ENABLED.load(Ordering::Acquire) || SERVER_EVENTS.load(Ordering::Acquire) || UNRELEASED.load(Ordering::Acquire)
}
pub static LOADED: AtomicBool = AtomicBool::new(false);
static INSTALLED: OnceLock<Result<(), String>> = OnceLock::new();
static MASTER_HOOK: OnceLock<Result<(), String>> = OnceLock::new();
static STATE: Mutex<Option<Pack>> = Mutex::new(None);
static STATUS: Mutex<String> = Mutex::new(String::new());
static OPEN: AtomicUsize = AtomicUsize::new(0);
static PATHS: [AtomicUsize; 4] = [const { AtomicUsize::new(0) }; 4];
static DOWNLOAD: AtomicUsize = AtomicUsize::new(0);
static DOWNLOAD_LIST: AtomicUsize = AtomicUsize::new(0);
static LOAD: AtomicUsize = AtomicUsize::new(0);
static ASYNC_LOAD: AtomicUsize = AtomicUsize::new(0);
static EXISTS: AtomicUsize = AtomicUsize::new(0);
type Invoke = unsafe extern "C" fn(*const c_void, *mut c_void, *mut *mut c_void, *mut *mut c_void) -> *mut c_void;
type FromName = unsafe extern "C" fn(*mut c_void, *const c_char, i32) -> *const c_void;
type OpenFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *mut c_void, i32, *const c_void) -> bool;
type PathFn = unsafe extern "C" fn(i32, *const c_void) -> *mut c_void;
type LoadFn = unsafe extern "C" fn(*mut c_void, *mut c_void, i32, bool, *const c_void) -> *mut c_void;
type AsyncFn = unsafe extern "C" fn(*mut c_void, *mut c_void, i32, *mut c_void, bool, *const c_void);

struct CardArt { donor: i32, texture: usize, sprite: usize, texture_handle: u32, sprite_handle: u32, slots: Vec<String>, slot_art: HashMap<usize, (usize, usize)> }
struct Pack { manifest: Value, dir: PathBuf, server: String, token: String, expires: u64, instance: String, cards: HashMap<i32, CardArt> }

fn status(s: impl Into<String>) { *STATUS.lock().unwrap_or_else(|e| e.into_inner()) = s.into(); }
fn region_name() -> &'static str { if region() == Region::Jp { "jp" } else { "global" } }
fn server() -> Option<String> {
    let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
    (cfg.enabled && enabled() && !OFFICIAL_ORIGINS.iter().any(|o| cfg.server.starts_with(o)))
        .then(|| cfg.server.clone())
}

pub fn install() {
    let result = MASTER_HOOK.get_or_init(|| unsafe {
        let connection = class(c"LibNative.Runtime.dll", c"LibNative.Sqlite3", c"Connection")?;
        hook(method(connection, c"Open", 4)?, hk_open as *mut c_void, &OPEN)
    });
    if let Err(e) = result { status(format!("Unavailable: {e}. Custom cards stay hidden.")); log(2, &format!("custom support: {e}")); }
    else { status("Waiting for the game's master connection. Restart if already logged in."); log(3, "custom support: master-open hook installed; managed setup deferred"); }
}

pub(crate) unsafe fn class(image: &CStr, ns: &CStr, name: &CStr) -> Result<*mut c_void, String> {
    let api = API.get().ok_or("No plugin API")?;
    let image = api.get_image.ok_or("No image lookup")?(image.as_ptr());
    if image.is_null() { return Err("Required client assembly missing".into()); }
    let c = api.get_class.ok_or("No class lookup")?(image, ns.as_ptr(), name.as_ptr());
    if c.is_null() { Err(format!("Client class {} missing", name.to_string_lossy())) } else { Ok(c) }
}

pub(crate) unsafe fn method(c: *mut c_void, name: &CStr, n: i32) -> Result<*const c_void, String> {
    let f: FromName = il2cpp("il2cpp_class_get_method_from_name").ok_or("No method lookup")?;
    let m = f(c, name.as_ptr(), n);
    if m.is_null() { Err(format!("Method {}({n}) missing", name.to_string_lossy())) } else { Ok(m) }
}

pub(crate) unsafe fn hook(m: *const c_void, detour: *mut c_void, slot: &AtomicUsize) -> Result<(), String> {
    let addr = *(m as *const usize);
    if addr == 0 { return Err("Client method has no compiled body".into()); }
    let t = interceptor_hook(addr, detour);
    if t == 0 { return Err("Could not install custom support hook".into()); }
    slot.store(t, Ordering::Release);
    Ok(())
}

pub(crate) unsafe fn invoke(m: *const c_void, obj: *mut c_void, args: &mut [*mut c_void]) -> Result<*mut c_void, String> {
    let f: Invoke = il2cpp("il2cpp_runtime_invoke").ok_or("No runtime invoke")?;
    let mut exc = std::ptr::null_mut();
    let r = f(m, obj, if args.is_empty() { std::ptr::null_mut() } else { args.as_mut_ptr() }, &mut exc);
    if !exc.is_null() { Err("Client rejected custom support operation".into()) } else { Ok(r) }
}

unsafe fn inflate(c: *mut c_void, name: &CStr, n: i32) -> Result<*const c_void, String> {
    type GetObject = unsafe extern "C" fn(*const c_void, *mut c_void) -> *mut c_void;
    type GetType = unsafe extern "C" fn(*mut c_void) -> *const c_void;
    type TypeObject = unsafe extern "C" fn(*const c_void) -> *mut c_void;
    type Array = unsafe extern "C" fn(*mut c_void, usize) -> *mut c_void;
    type Reflection = unsafe extern "C" fn(*mut c_void) -> *const c_void;
    let get: GetObject = il2cpp("il2cpp_method_get_object").ok_or("No reflection")?;
    let get_type: GetType = il2cpp("il2cpp_class_get_type").ok_or("No type lookup")?;
    let type_obj: TypeObject = il2cpp("il2cpp_type_get_object").ok_or("No type object")?;
    let array: Array = il2cpp("il2cpp_array_new").ok_or("No array allocation")?;
    let from: Reflection = il2cpp("il2cpp_method_get_from_reflection").ok_or("No method reflection")?;
    let generic = method(c, name, n)?;
    let refl = get(generic, c);
    if refl.is_null() { return Err("Generic loader reflection missing".into()); }
    let make = method(*(refl as *const *mut c_void), c"MakeGenericMethod", 1)?;
    let typ = class(c"mscorlib.dll", c"System", c"Type")?;
    let obj = class(c"UnityEngine.CoreModule.dll", c"UnityEngine", c"Texture2D")?;
    let arr = array(typ, 1);
    if arr.is_null() { return Err("Cannot allocate generic argument".into()); }
    *((arr as *mut u8).add(0x20) as *mut *mut c_void) = type_obj(get_type(obj));
    let inflated = invoke(make, refl, &mut [arr])?;
    if inflated.is_null() { return Err("Generic loader is unavailable".into()); }
    let m = from(inflated);
    if m.is_null() { Err("Generic method has no runtime definition".into()) } else { Ok(m) }
}

unsafe fn install_hooks() -> Result<(), String> {
    let current: unsafe extern "C" fn() -> *mut c_void =
        il2cpp("il2cpp_thread_current").ok_or("No managed-thread lookup")?;
    if current().is_null() { return Err("Master opened outside a managed game thread".into()); }
    log(3, "custom support: resolving resource hooks on the managed master-open thread");
    let connection = class(c"LibNative.Runtime.dll", c"LibNative.Sqlite3", c"Connection")?;
    let resource = class(c"umamusume.dll", c"Gallop", c"ResourcePath")?;
    let names = [c"GetSupportCardTexTexturePath", c"GetSupportStillTexTexturePath", c"GetSupportCardThumbnailIconPath", c"GetSupportCardSTexturePath"];
    let paths: Vec<_> = names.iter().map(|n| method(resource, n, 1)).collect::<Result<_, _>>()?;
    let work = class(c"umamusume.dll", c"Gallop", c"WorkSupportCardData")?;
    let download = method(work, c"RegisterDownloadAssets", 2)?;
    let download_list = method(work, c"ListUpSupportCardAssetbundlePath", 1)?;
    let manager = class(c"umamusume.dll", c"Gallop", c"ResourceManager")?;
    let load = inflate(manager, c"Load", 3)?;
    let async_load = inflate(manager, c"LoadAsync", 4)?;
    let exists = method(manager, c"IsExistAsset", 1)?;
    method(connection, c"Query", 1)?;
    let query = class(c"LibNative.Runtime.dll", c"LibNative.Sqlite3", c"Query")?;
    method(query, c"Step", 0)?; method(query, c"GetInt", 1)?; method(query, c"Dispose", 0)?;
    class(c"UnityEngine.CoreModule.dll", c"UnityEngine", c"Texture2D")?;
    method(class(c"UnityEngine.ImageConversionModule.dll", c"UnityEngine", c"ImageConversion")?, c"LoadImage", 3)?;
    il2cpp::<unsafe extern "C" fn(*mut c_void, bool) -> u32>("il2cpp_gchandle_new").ok_or("No GC rooting")?;
    let detours = [path0 as *mut c_void, path1 as *mut c_void, path2 as *mut c_void, path3 as *mut c_void];
    for i in 0..4 { hook(paths[i], detours[i], &PATHS[i])?; }
    hook(download, hk_download as *mut c_void, &DOWNLOAD)?;
    hook(download_list, hk_download_list as *mut c_void, &DOWNLOAD_LIST)?;
    hook(load, hk_load as *mut c_void, &LOAD)?;
    hook(async_load, hk_async as *mut c_void, &ASYNC_LOAD)?;
    hook(exists, hk_exists as *mut c_void, &EXISTS)?;
    log(3, "custom support: resource hooks ready");
    status("Ready to load a pack on the next master open. Restart if already logged in.");
    Ok(())
}

unsafe extern "C" fn hk_open(this: *mut c_void, file: *mut c_void, vfs: *mut c_void, key: *mut c_void, cipher: i32, mi: *const c_void) -> bool {
    let original: OpenFn = std::mem::transmute(trampoline(&OPEN));
    let input = il2cpp_text(file).unwrap_or_default();
    if !input.replace('\\', "/").ends_with("/master/master.mdb") {
        return original(this, file, vfs, key, cipher, mi);
    }
    if LOADED.load(Ordering::Acquire) {
        let path = STATE.lock().unwrap_or_else(|e| e.into_inner()).as_ref()
            .map(|p| p.dir.join("master.mdb"));
        if let (Some(path), Some(new)) = (path, API.get().and_then(|a| a.string_new)) {
            let path = cstr(&path.to_string_lossy());
            return original(this, new(path.as_ptr()), vfs, key, cipher, mi);
        }
        return original(this, file, vfs, key, cipher, mi);
    }
    if server().is_none() {
        return original(this, file, vfs, key, cipher, mi);
    }
    if let Err(e) = INSTALLED.get_or_init(|| install_hooks()) {
        status(format!("Custom cards hidden: {e}"));
        log(2, &format!("custom support: managed setup refused: {e}"));
        return original(this, file, vfs, key, cipher, mi);
    }
    let result = std::panic::catch_unwind(|| prepare(&input));
    let mut pack = match result {
        Ok(Ok(p)) => p,
        Ok(Err(e)) => { status(format!("Custom cards hidden: {e}")); log(2, &format!("custom support: {e}")); return original(this, file, vfs, key, cipher, mi); },
        Err(_) => { status("Custom pack preparation failed; using stock master."); return original(this, file, vfs, key, cipher, mi); }
    };
    let Some(new_string) = API.get().and_then(|a| a.string_new) else { return original(this, file, vfs, key, cipher, mi) };
    let path = cstr(&pack.dir.join("master.mdb").to_string_lossy());
    let custom_file = new_string(path.as_ptr());
    if custom_file.is_null() { return original(this, file, vfs, key, cipher, mi); }
    let ok = original(this, custom_file, vfs, key, cipher, mi);
    if !ok { status("Custom master could not open; using stock master."); return original(this, file, vfs, key, cipher, mi); }
    LOADED.store(true, Ordering::Release);
    let ready = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| verify_ready(this, &mut pack)));
    match ready {
        Ok(Ok(())) => status(format!("{} custom cards ready · {}", pack.cards.len(), &pack.manifest["digest"].as_str().unwrap_or("")[..12])),
        Ok(Err(e)) => { pack.token.clear(); status(format!("Custom cards hidden: {e}. Restart after fixing the pack.")); },
        Err(_) => { pack.token.clear(); status("Custom readiness failed. Restart required."); }
    }
    *STATE.lock().unwrap_or_else(|e| e.into_inner()) = Some(pack);
    ok
}

fn prepare(base_path: &str) -> Result<Pack, String> {
    let server = server().ok_or("Custom cards disabled")?;
    status("Checking client/server master compatibility…");
    let base = sha256(&std::fs::read(base_path).map_err(|e| e.to_string())?);
    let body = json!({"protocol":1,"region":region_name(),"base_sha256":base}).to_string();
    let bytes = http(&server, "POST", "/custom-support/bootstrap", body.as_bytes())?;
    let manifest: Value = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
    let digest = manifest["digest"].as_str().ok_or("Missing pack digest")?;
    if digest.len() != 64 || !digest.bytes().all(|b| b.is_ascii_hexdigit()) || manifest["region"] != region_name() || manifest["base_sha256"] != base || manifest["protocol"] != 1 {
        return Err("Incompatible pack manifest".into());
    }
    let challenge = manifest["challenge"].as_str().ok_or("Missing challenge")?;
    let dir = config_path().ok_or("Plugin directory unavailable")?.parent().unwrap().join("custom_support").join(digest);
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    download_verified(&server, challenge, "master.mdb", manifest["master_sha256"].as_str().ok_or("Missing master checksum")?, &dir)?;
    let mut cards = HashMap::new();
    for c in manifest["cards"].as_array().ok_or("Missing cards")? {
        let id = c["id"].as_i64().ok_or("Invalid card ID")? as i32;
        let donor = c["art_donor"].as_i64().ok_or("Missing art donor")? as i32;
        let art = c["art_sha256"].as_str().unwrap_or("");
        if !art.is_empty() { download_verified(&server, challenge, &format!("{id}.png"), art, &dir)?; }
        let mut slots = Vec::new();
        if let Some(map) = c["slot_art"].as_object() {
            for (slot, sha) in map {
                let name = format!("{id}_{slot}.png");
                download_verified(&server, challenge, &name, sha.as_str().ok_or("Invalid slot art")?, &dir)?;
                slots.push(slot.clone());
            }
        }
        cards.insert(id, CardArt { donor, texture:0, sprite:0, texture_handle:0, sprite_handle:0, slots, slot_art: HashMap::new() });
    }
    let events = manifest["events"].as_array();
    if !cards.is_empty() && !ENABLED.load(Ordering::Acquire) {
        return Err("This pack contains custom cards. Enable custom supports and restart.".into());
    }
    let charas = manifest["charas"].as_array();
    let scenarios = manifest["scenarios"].as_array();
    let unreleased = charas.is_some_and(|c| !c.is_empty()) || scenarios.is_some_and(|s| !s.is_empty());
    if cards.is_empty() && events.is_none_or(|e| e.is_empty()) && !unreleased {
        return Err("Empty private master pack".into());
    }
    if events.is_some_and(|e| !e.is_empty()) && !SERVER_EVENTS.load(Ordering::Acquire) {
        return Err("This pack contains server events. Enable server events and restart.".into());
    }
    if unreleased && !UNRELEASED.load(Ordering::Acquire) {
        return Err("This pack contains unreleased trainees or scenarios. Enable them and restart.".into());
    }
    let instance = format!("{}-{}-{}", std::process::id(), epoch(), GENERATION.load(Ordering::Acquire));
    Ok(Pack { manifest, dir, server, token:String::new(), expires:0, instance, cards })
}

fn download_verified(server: &str, challenge: &str, name: &str, expected: &str, dir: &std::path::Path) -> Result<(), String> {
    let path = dir.join(name);
    if std::fs::read(&path).is_ok_and(|b| sha256(&b) == expected) { return Ok(()); }
    status(format!("Downloading custom support {name}…"));
    let bytes = http(server, "GET", &format!("/custom-support/pack/{challenge}/{name}"), &[])?;
    if sha256(&bytes) != expected { return Err(format!("Checksum mismatch for {name}")); }
    let tmp = path.with_extension("pending");
    std::fs::write(&tmp, bytes).map_err(|e| e.to_string())?;
    if path.exists() { std::fs::remove_file(&path).map_err(|e| e.to_string())?; }
    std::fs::rename(tmp, path).map_err(|e| e.to_string())
}

unsafe fn verify_ready(connection: *mut c_void, p: &mut Pack) -> Result<(), String> {
    let conn = class(c"LibNative.Runtime.dll", c"LibNative.Sqlite3", c"Connection")?;
    let query_class = class(c"LibNative.Runtime.dll", c"LibNative.Sqlite3", c"Query")?;
    let query_method = method(conn, c"Query", 1)?;
    let step = method(query_class, c"Step", 0)?;
    let get_int = method(query_class, c"GetInt", 1)?;
    let dispose = method(query_class, c"Dispose", 0)?;
    let string = API.get().and_then(|a| a.string_new).ok_or("No strings")?;
    for (&id, art) in &mut p.cards {
        let sql = cstr(&format!("SELECT id FROM support_card_data WHERE id={id}"));
        let q = invoke(query_method, connection, &mut [string(sql.as_ptr())])?;
        if q.is_null() { return Err("Custom card lookup failed".into()); }
        let probe: Result<(), String> = (|| {
            let stepped = invoke(step, q, &mut [])?;
            if stepped.is_null() || *((stepped as *const u8).add(0x10)) == 0 { return Err("Custom card is missing from the opened master".into()); }
            let mut index = 0i32;
            let value = invoke(get_int, q, &mut [&mut index as *mut i32 as *mut c_void])?;
            if value.is_null() || *((value as *const u8).add(0x10) as *const i32) != id { return Err("Custom master row mismatch".into()); }
            Ok(())
        })();
        let _ = invoke(dispose, q, &mut []);
        probe?;
        if p.dir.join(format!("{id}.png")).exists() {
            let bytes = std::fs::read(p.dir.join(format!("{id}.png"))).map_err(|e| e.to_string())?;
            let (texture, sprite, th, sh) = decode_texture(&bytes)?;
            art.texture = texture as usize; art.sprite = sprite as usize;
            art.texture_handle = th; art.sprite_handle = sh;
        }
        for slot in art.slots.clone() {
            let Ok(index) = slot.parse::<usize>() else { continue };
            let bytes = std::fs::read(p.dir.join(format!("{id}_{slot}.png"))).map_err(|e| e.to_string())?;
            let (texture, sprite, _, _) = decode_texture(&bytes)?;
            art.slot_art.insert(index, (texture as usize, sprite as usize));
        }
    }
    for event in p.manifest["events"].as_array().into_iter().flatten() {
        let table = match event["family"].as_str() {
            Some("training_challenge") => "training_challenge_master",
            Some("story_event") => "story_event_data",
            Some("factor_research") => "factor_research_data",
            Some("champions") => "champions_schedule",
            Some("legend_race") => "legend_race",
            Some("heroes") => "heroes_data",
            Some("challenge_match") => "challenge_match_data",
            Some("team_building") => "team_building_data",
            Some("map_event") => "map_event_data",
            _ => return Err("Unsupported server event family".into()),
        };
        let key = match table {
            "story_event_data" => "story_event_id",
            "factor_research_data" => "factor_research_event_id",
            "heroes_data" => "heroes_id",
            "challenge_match_data" => "challenge_match_id",
            "team_building_data" => "team_building_event_id",
            "map_event_data" => "event_id",
            _ => "id",
        };
        let id = event["event_id"].as_i64().ok_or("Missing event ID")?;
        let start = event["start_date"].as_u64().ok_or("Missing event start")?;
        let end = event["end_date"].as_u64().ok_or("Missing event end")?;
        let sql = cstr(&format!("SELECT {key} FROM {table} WHERE {key}={id} AND start_date={start} AND {}={end}",
            if table == "training_challenge_master" { "end_result_date" } else { "end_date" }));
        let q = invoke(query_method, connection, &mut [string(sql.as_ptr())])?;
        if q.is_null() { return Err("Event schedule lookup failed".into()); }
        let probe = invoke(step, q, &mut []);
        let _ = invoke(dispose, q, &mut []);
        let value = probe?;
        if value.is_null() || *((value as *const u8).add(0x10)) == 0 {
            return Err("Event schedule is missing from the opened master".into());
        }
    }
    let has_row = |sql: String| -> Result<bool, String> {
        let sql = cstr(&sql);
        let q = invoke(query_method, connection, &mut [string(sql.as_ptr())])?;
        if q.is_null() { return Err("Master lookup failed".into()); }
        let probe = invoke(step, q, &mut []);
        let _ = invoke(dispose, q, &mut []);
        let value = probe?;
        Ok(!value.is_null() && *((value as *const u8).add(0x10)) != 0)
    };
    for card in p.manifest["charas"].as_array().into_iter().flatten().flat_map(|c| c["cards"].as_array().into_iter().flatten()) {
        let id = card.as_i64().ok_or("Invalid trainee card ID")?;
        if !has_row(format!("SELECT id FROM card_data WHERE id={id}"))? {
            return Err("Unreleased trainee is missing from the opened master".into());
        }
    }
    for scenario in p.manifest["scenarios"].as_array().into_iter().flatten() {
        let id = scenario["scenario_id"].as_i64().ok_or("Invalid scenario ID")?;
        if !has_row(format!("SELECT id FROM single_mode_scenario WHERE id={id}"))? {
            return Err("Unreleased scenario is missing from the opened master".into());
        }
    }
    let req = json!({"challenge":p.manifest["challenge"],"digest":p.manifest["digest"],
        "master_sha256":p.manifest["master_sha256"],"instance":p.instance}).to_string();
    let r: Value = serde_json::from_slice(&http(&p.server, "POST", "/custom-support/ready", req.as_bytes())?).map_err(|e| e.to_string())?;
    p.token = r["token"].as_str().ok_or("Readiness was refused")?.to_owned();
    p.expires = r["expires"].as_f64().ok_or("Missing capability expiry")? as u64;
    Ok(())
}

pub(crate) unsafe fn decode_texture(bytes: &[u8]) -> Result<(*mut c_void, *mut c_void, u32, u32), String> {
    type New = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type Array = unsafe extern "C" fn(*mut c_void, usize) -> *mut c_void;
    type Root = unsafe extern "C" fn(*mut c_void, bool) -> u32;
    let new: New = il2cpp("il2cpp_object_new").ok_or("No object allocation")?;
    let array: Array = il2cpp("il2cpp_array_new").ok_or("No array allocation")?;
    let root: Root = il2cpp("il2cpp_gchandle_new").ok_or("No texture rooting")?;
    let tc = class(c"UnityEngine.CoreModule.dll", c"UnityEngine", c"Texture2D")?;
    let ctor = method(tc, c".ctor", 2)?;
    let texture = new(tc);
    if texture.is_null() { return Err("Texture allocation failed".into()); }
    let mut width = 2i32; let mut height = 2i32;
    invoke(ctor, texture, &mut [&mut width as *mut i32 as *mut c_void, &mut height as *mut i32 as *mut c_void])?;
    let bc = class(c"mscorlib.dll", c"System", c"Byte")?;
    let data = array(bc, bytes.len());
    if data.is_null() { return Err("Image buffer allocation failed".into()); }
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), (data as *mut u8).add(0x20), bytes.len());
    let image = class(c"UnityEngine.ImageConversionModule.dll", c"UnityEngine", c"ImageConversion")?;
    let mut nonreadable = false;
    let result = invoke(method(image, c"LoadImage", 3)?, std::ptr::null_mut(), &mut [texture, data, &mut nonreadable as *mut bool as *mut c_void])?;
    if result.is_null() || *((result as *const u8).add(0x10)) == 0 { return Err("PNG texture decoding failed".into()); }
    let w = invoke(method(tc, c"get_width", 0)?, texture, &mut [])?;
    let h = invoke(method(tc, c"get_height", 0)?, texture, &mut [])?;
    if w.is_null() || h.is_null() { return Err("Texture dimensions unavailable".into()); }
    let mut rect = [0f32, 0f32, *((w as *const u8).add(0x10) as *const i32) as f32, *((h as *const u8).add(0x10) as *const i32) as f32];
    let mut pivot = [0.5f32, 0.5f32];
    let sc = class(c"UnityEngine.CoreModule.dll", c"UnityEngine", c"Sprite")?;
    let sprite = invoke(method(sc, c"Create", 3)?, std::ptr::null_mut(), &mut [texture, rect.as_mut_ptr() as *mut c_void, pivot.as_mut_ptr() as *mut c_void])?;
    if sprite.is_null() { return Err("Sprite creation failed".into()); }
    let hide = method(class(c"UnityEngine.CoreModule.dll", c"UnityEngine", c"Object")?, c"set_hideFlags", 1)?;
    for obj in [texture, sprite] {
        let mut flags = 32i32;
        invoke(hide, obj, &mut [&mut flags as *mut i32 as *mut c_void])?;
    }
    Ok((texture, sprite, root(texture, false), root(sprite, false)))
}

unsafe fn resource_path(id: i32, mi: *const c_void, i: usize) -> *mut c_void {
    let original: PathFn = std::mem::transmute(trampoline(&PATHS[i]));
    let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(art) = state.as_ref().and_then(|s| s.cards.get(&id)) {
        if art.texture != 0 {
            if let Some(new) = API.get().and_then(|a| a.string_new) {
                return new(cstr(&format!("umaps-custom/{id}/{i}")).as_ptr());
            }
        }
        let donor = art.donor;
        drop(state);
        return original(donor, mi);
    }
    drop(state);
    original(id, mi)
}
unsafe extern "C" fn path0(id: i32, mi: *const c_void) -> *mut c_void { resource_path(id, mi, 0) }
unsafe extern "C" fn path1(id: i32, mi: *const c_void) -> *mut c_void { resource_path(id, mi, 1) }
unsafe extern "C" fn path2(id: i32, mi: *const c_void) -> *mut c_void { resource_path(id, mi, 2) }
unsafe extern "C" fn path3(id: i32, mi: *const c_void) -> *mut c_void { resource_path(id, mi, 3) }

unsafe extern "C" fn hk_download(register: *mut c_void, id: i32, mi: *const c_void) {
    type Fn = unsafe extern "C" fn(*mut c_void, i32, *const c_void);
    let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    let donor = state.as_ref().and_then(|s| s.cards.get(&id)).map(|a| a.donor).unwrap_or(id);
    drop(state);
    std::mem::transmute::<usize, Fn>(trampoline(&DOWNLOAD))(register, donor, mi);
}

unsafe extern "C" fn hk_download_list(id: i32, mi: *const c_void) -> *mut c_void {
    let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    let donor = state.as_ref().and_then(|s| s.cards.get(&id)).map(|a| a.donor).unwrap_or(id);
    drop(state);
    std::mem::transmute::<usize, PathFn>(trampoline(&DOWNLOAD_LIST))(donor, mi)
}

fn custom_id(path: *mut c_void) -> Option<i32> {
    let s = il2cpp_text(path)?;
    s.strip_prefix("umaps-custom/")?.split('/').next()?.parse().ok()
}

fn custom_slot(path: *mut c_void) -> Option<usize> {
    let s = il2cpp_text(path)?;
    s.strip_prefix("umaps-custom/")?.split('/').nth(1)?.parse().ok()
}

fn slot_objects(art: &CardArt, slot: Option<usize>) -> (usize, usize) {
    slot.and_then(|i| art.slot_art.get(&i).copied()).unwrap_or((art.texture, art.sprite))
}

unsafe fn local_object(path: *mut c_void, mi: *const c_void) -> Option<*mut c_void> {
    let id = custom_id(path)?;
    let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
    let art = state.as_ref()?.cards.get(&id)?;
    type ReturnType = unsafe extern "C" fn(*const c_void) -> *const c_void;
    type TypeClass = unsafe extern "C" fn(*const c_void) -> *mut c_void;
    type Name = unsafe extern "C" fn(*mut c_void) -> *const c_char;
    let return_type: ReturnType = il2cpp("il2cpp_method_get_return_type")?;
    let type_class: TypeClass = il2cpp("il2cpp_class_from_type")?;
    let name: Name = il2cpp("il2cpp_class_get_name")?;
    let c = type_class(return_type(mi));
    if c.is_null() { return None; }
    let n = name(c);
    if n.is_null() { return None; }
    let (texture, sprite) = slot_objects(art, custom_slot(path));
    let ptr = if CStr::from_ptr(n).to_bytes() == b"Sprite" { sprite } else { texture };
    (ptr != 0).then_some(ptr as *mut c_void)
}

unsafe extern "C" fn hk_load(this: *mut c_void, path: *mut c_void, hash: i32, lower: bool, mi: *const c_void) -> *mut c_void {
    if let Some(obj) = local_object(path, mi) { return obj; }
    std::mem::transmute::<usize, LoadFn>(trampoline(&LOAD))(this, path, hash, lower, mi)
}

unsafe extern "C" fn hk_async(this: *mut c_void, path: *mut c_void, hash: i32, callback: *mut c_void, lower: bool, mi: *const c_void) {
    if (custom_id(path).is_some()) && !callback.is_null() {
        let c = *(callback as *const *mut c_void);
        if let Ok(m) = method(c, c"Invoke", 1) {
            type Param = unsafe extern "C" fn(*const c_void, u32) -> *const c_void;
            type TypeClass = unsafe extern "C" fn(*const c_void) -> *mut c_void;
            type Name = unsafe extern "C" fn(*mut c_void) -> *const c_char;
            if let (Some(param), Some(tc), Some(name)) = (il2cpp::<Param>("il2cpp_method_get_param"), il2cpp::<TypeClass>("il2cpp_class_from_type"), il2cpp::<Name>("il2cpp_class_get_name")) {
                let cls = tc(param(m, 0));
                if !cls.is_null() {
                    let n = name(cls);
                    let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
                    let art = custom_id(path).and_then(|id| state.as_ref()?.cards.get(&id));
                    if let Some(art) = art {
                        let (texture, sprite) = slot_objects(art, custom_slot(path));
                        let obj = if !n.is_null() && CStr::from_ptr(n).to_bytes() == b"Sprite" { sprite } else { texture } as *mut c_void;
                        drop(state);
                        if !obj.is_null() && invoke(m, callback, &mut [obj]).is_ok() { return; }
                    }
                }
            }
        }
    }
    std::mem::transmute::<usize, AsyncFn>(trampoline(&ASYNC_LOAD))(this, path, hash, callback, lower, mi)
}

unsafe extern "C" fn hk_exists(path: *mut c_void, mi: *const c_void) -> bool {
    type Fn = unsafe extern "C" fn(*mut c_void, *const c_void) -> bool;
    if custom_id(path).is_some() { return true; }
    std::mem::transmute::<usize, Fn>(trampoline(&EXISTS))(path, mi)
}

pub unsafe fn add_header(headers: *mut c_void) {
    if headers.is_null() || server().is_none() { return; }
    let (mut token, origin, expires) = {
        let state = STATE.lock().unwrap_or_else(|e| e.into_inner());
        let Some(p) = state.as_ref() else { return };
        if p.token.is_empty() || p.expires <= epoch() || server().as_deref() != Some(p.server.as_str()) { return; }
        (p.token.clone(), p.server.clone(), p.expires)
    };
    if expires < epoch() + 3600 {
        let request = json!({"token":token}).to_string();
        let renewed = http(&origin, "POST", "/custom-support/renew", request.as_bytes())
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).map_err(|e| e.to_string()));
        let mut state = STATE.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(p) = state.as_mut() {
            if p.token == token {
                match renewed {
                    Ok(r) if r["token"] == token && r["expires"].as_f64().is_some() => p.expires = r["expires"].as_f64().unwrap() as u64,
                    _ => { p.token.clear(); token.clear(); status("Readiness expired or server restarted. Custom cards hidden; restart the client."); }
                }
            }
        }
    }
    if token.is_empty() { return; }
    let Some(new) = API.get().and_then(|a| a.string_new) else { return };
    let c = *(headers as *const *mut c_void);
    if let Ok(set) = method(c, c"set_Item", 2) {
        let _ = invoke(set, headers, &mut [new(c"X-Uma-Custom-Support".as_ptr()), new(cstr(&token).as_ptr())]);
    }
}

pub fn menu(u: &Ui<'_>) {
    u.separator(); u.heading("UmaPack");
    if let Some(cb) = u.api.checkbox {
        let all = [&ENABLED, &SERVER_EVENTS, &UNRELEASED];
        let was = all.iter().all(|f| f.load(Ordering::Acquire));
        let mut on = was;
        unsafe { cb(u.ui, c"Enable UmaPack (restart required)".as_ptr(), &mut on) };
        if on != was {
            for f in all { f.store(on, Ordering::Release); }
            save_config(&CONFIG.read().unwrap_or_else(|e| e.into_inner()));
            status("UmaPack setting saved. Restart the game to apply it.");
        }
        if !was && all.iter().any(|f| f.load(Ordering::Acquire)) { u.small("Partly on (set in redirect.cfg)."); }
    }
    u.small("Custom support cards, server events and unreleased trainees / scenarios.");
    u.small("Optional. Requires a matching published pack on your private server.");
    let text = STATUS.lock().unwrap_or_else(|e| e.into_inner()).clone();
    if !text.is_empty() { u.small(&text); }
    if LOADED.load(Ordering::Acquire) { u.small("A private master is loaded. Restart before changing servers."); }
}

fn epoch() -> u64 { std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_secs() }

#[link(name="winhttp")]
extern "system" {
    fn WinHttpOpen(agent:*const u16, access:u32, proxy:*const u16, bypass:*const u16, flags:u32)->*mut c_void;
    fn WinHttpConnect(session:*mut c_void, server:*const u16, port:u16, reserved:u32)->*mut c_void;
    fn WinHttpOpenRequest(conn:*mut c_void, verb:*const u16, object:*const u16, version:*const u16, referer:*const u16, accept:*const *const u16, flags:u32)->*mut c_void;
    fn WinHttpSetOption(handle:*mut c_void, option:u32, buffer:*mut c_void, len:u32)->i32;
    fn WinHttpSetTimeouts(handle:*mut c_void, resolve:i32, connect:i32, send:i32, receive:i32)->i32;
    fn WinHttpSendRequest(req:*mut c_void, headers:*const u16, len:u32, body:*mut c_void, size:u32, total:u32, context:usize)->i32;
    fn WinHttpReceiveResponse(req:*mut c_void, reserved:*mut c_void)->i32;
    fn WinHttpQueryHeaders(req:*mut c_void, level:u32, name:*const u16, buffer:*mut c_void, len:*mut u32, index:*mut u32)->i32;
    fn WinHttpReadData(req:*mut c_void, buffer:*mut c_void, count:u32, read:*mut u32)->i32;
    fn WinHttpCloseHandle(handle:*mut c_void)->i32;
}
struct Handle(*mut c_void);
impl Drop for Handle { fn drop(&mut self) { if !self.0.is_null() { unsafe { WinHttpCloseHandle(self.0); } } } }
fn wide(s:&str)->Vec<u16> { s.encode_utf16().chain(std::iter::once(0)).collect() }
fn http(origin:&str, verb:&str, path:&str, body:&[u8])->Result<Vec<u8>,String> {
    if server().as_deref() != Some(origin) { return Err("Private server changed".into()); }
    http_to(origin, verb, path, body)
}
pub(crate) fn http_to(origin:&str, verb:&str, path:&str, body:&[u8])->Result<Vec<u8>,String> {
    http_to_t(origin,verb,path,body,[5000,5000,10000,30000])
}

pub(crate) fn http_to_t(origin:&str, verb:&str, path:&str, body:&[u8], t:[i32;4])->Result<Vec<u8>,String> {
    let rest = origin.strip_prefix("https://").ok_or("HTTPS is required")?;
    let (authority,prefix) = rest.split_once('/').unwrap_or((rest,""));
    let (host,port) = authority.rsplit_once(':').map(|(h,p)| Ok::<_,String>((h,p.parse::<u16>().map_err(|_| "Invalid server port")?))).unwrap_or(Ok((authority,443)))?;
    if host.is_empty() || host.contains('[') { return Err("Custom support bootstrap currently requires a hostname or IPv4 address".into()); }
    unsafe {
        let session = Handle(WinHttpOpen(wide("UmaPS Custom Supports/1").as_ptr(),0,std::ptr::null(),std::ptr::null(),0));
        if session.0.is_null() { return Err("Cannot initialize pack download".into()); }
        WinHttpSetTimeouts(session.0,t[0],t[1],t[2],t[3]);
        let conn = Handle(WinHttpConnect(session.0,wide(host).as_ptr(),port,0));
        if conn.0.is_null() { return Err("Cannot connect to private server".into()); }
        let target = if prefix.is_empty() {path.to_string()} else {format!("/{prefix}{path}")};
        let req = Handle(WinHttpOpenRequest(conn.0,wide(verb).as_ptr(),wide(&target).as_ptr(),std::ptr::null(),std::ptr::null(),std::ptr::null(),0x800000));
        if req.0.is_null() { return Err("Cannot request custom pack".into()); }
        let mut security = 0x3300u32;
        let mut redirects = 0u32;
        WinHttpSetOption(req.0,31,&mut security as *mut _ as *mut c_void,4);
        WinHttpSetOption(req.0,88,&mut redirects as *mut _ as *mut c_void,4);
        let headers=wide("Content-Type: application/json\r\nX-Uma-Custom-Plugin: 1\r\n");
        if WinHttpSendRequest(req.0,headers.as_ptr(),u32::MAX,body.as_ptr() as *mut c_void,body.len() as u32,body.len() as u32,0)==0 || WinHttpReceiveResponse(req.0,std::ptr::null_mut())==0 { return Err("Private server did not answer pack bootstrap".into()); }
        let mut code=0u32; let mut len=4u32;
        if WinHttpQueryHeaders(req.0,19|0x20000000,std::ptr::null(),&mut code as *mut _ as *mut c_void,&mut len,std::ptr::null_mut())==0 { return Err("Missing HTTP status".into()); }
        let mut out=Vec::new(); let mut buf=[0u8;65536];
        loop { let mut n=0u32; if WinHttpReadData(req.0,buf.as_mut_ptr() as *mut c_void,buf.len() as u32,&mut n)==0 {return Err("Pack download interrupted".into());} if n==0 {break;} if out.len()+n as usize>128*1024*1024 {return Err("Custom pack too large".into());} out.extend_from_slice(&buf[..n as usize]); }
        if code!=200 { let detail=serde_json::from_slice::<Value>(&out).ok().and_then(|v| v["detail"].as_str().map(str::to_owned)).unwrap_or_else(||format!("Server refused custom pack ({code})")); return Err(detail); }
        Ok(out)
    }
}

pub(crate) fn sha256(data:&[u8])->String {
    const K:[u32;64]=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
    let mut h=[0x6a09e667u32,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
    let mut padded=data.to_vec(); padded.push(0x80); while padded.len()%64!=56 {padded.push(0);} padded.extend_from_slice(&((data.len() as u64)*8).to_be_bytes());
    for block in padded.chunks_exact(64) {
        let mut w=[0u32;64]; for i in 0..16 {w[i]=u32::from_be_bytes(block[i*4..i*4+4].try_into().unwrap());}
        for i in 16..64 {let a=w[i-15];let b=w[i-2];w[i]=(a.rotate_right(7)^a.rotate_right(18)^(a>>3)).wrapping_add(w[i-16]).wrapping_add(b.rotate_right(17)^b.rotate_right(19)^(b>>10)).wrapping_add(w[i-7]);}
        let [mut a,mut b,mut c,mut d,mut e,mut f,mut g,mut z]=h;
        for i in 0..64 {let t1=z.wrapping_add(e.rotate_right(6)^e.rotate_right(11)^e.rotate_right(25)).wrapping_add((e&f)^(!e&g)).wrapping_add(K[i]).wrapping_add(w[i]);let t2=(a.rotate_right(2)^a.rotate_right(13)^a.rotate_right(22)).wrapping_add((a&b)^(a&c)^(b&c));z=g;g=f;f=e;e=d.wrapping_add(t1);d=c;c=b;b=a;a=t1.wrapping_add(t2);}
        for (v,n) in h.iter_mut().zip([a,b,c,d,e,f,g,z]) {*v=v.wrapping_add(n);}
    }
    h.iter().map(|v|format!("{v:08x}")).collect()
}
