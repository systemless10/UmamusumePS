use super::custom_support::{class, hook, method};
use super::*;
use serde_json::Value;
use std::collections::HashMap;
use std::fs::File;
use std::io::{Seek, SeekFrom};

const MAGIC: &[u8] = b"UMAPACK1";
const MAX_ATTACHED: usize = 8;

struct Pack { path: PathBuf, digest: String, db: PathBuf }
struct Entry { pack: usize, offset: u64, len: u64, ready: AtomicBool }
struct Packs { packs: Vec<Pack>, files: HashMap<[u8; 32], Entry>, cache: PathBuf, fingerprint: String }

static PACKS: OnceLock<Packs> = OnceLock::new();
static OPEN_WRITABLE: AtomicUsize = AtomicUsize::new(0);
static LOCAL_PATH: AtomicUsize = AtomicUsize::new(0);
static DB_HANDLE: AtomicUsize = AtomicUsize::new(0);
static MOTION_CLIP: AtomicUsize = AtomicUsize::new(0);
static SOUND_PATH: AtomicUsize = AtomicUsize::new(0);
static SOUND_EXIST: AtomicUsize = AtomicUsize::new(0);
static EXTRACT: Mutex<()> = Mutex::new(());
static STATUS: Mutex<String> = Mutex::new(String::new());

fn status(s: impl Into<String>) {
    let s = s.into();
    log(3, &format!("umapack: {s}"));
    *STATUS.lock().unwrap_or_else(|e| e.into_inner()) = s;
}

pub fn install() {
    let Some(dir) = config_path().and_then(|p| p.parent().map(|d| d.join("umapacks"))) else { return };
    let mut paths: Vec<PathBuf> = std::fs::read_dir(&dir).into_iter().flatten().flatten()
        .map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "umapack")).collect();
    if paths.is_empty() { return; }
    paths.sort();
    if paths.len() > MAX_ATTACHED {
        log(2, &format!("umapack: {} packs found; only the first {MAX_ATTACHED} load", paths.len()));
        paths.truncate(MAX_ATTACHED);
    }
    let cache = dir.join(".cache");
    let mut packs = Vec::new();
    let mut files = HashMap::new();
    for path in paths {
        match read_pack(&path, &cache, packs.len(), &mut files) {
            Ok(p) => packs.push(p),
            Err(e) => log(2, &format!("umapack: {} skipped: {e}", path.display())),
        }
    }
    if packs.is_empty() { return; }
    let fingerprint = packs.iter().map(|p| &p.digest[..16]).collect::<Vec<_>>().join(",");
    let n = (packs.len(), files.len());
    let _ = PACKS.set(Packs { packs, files, cache, fingerprint });
    let hooked = unsafe { install_hooks() };
    match hooked {
        Ok(()) => status(format!("{} pack(s), {} files", n.0, n.1)),
        Err(e) => { status(format!("hooks unavailable: {e}")); return; }
    }
    std::thread::spawn(|| {
        let Some(p) = PACKS.get() else { return };
        let t = Instant::now();
        let mut done = 0usize;
        for (h, e) in &p.files {
            if ensure_file(p, h, e).is_some() { done += 1; }
        }
        log(3, &format!("umapack: {done}/{} files ready in {:.1}s", p.files.len(), t.elapsed().as_secs_f32()));
    });
}

fn read_pack(path: &std::path::Path, cache: &std::path::Path, index: usize, files: &mut HashMap<[u8; 32], Entry>) -> Result<Pack, String> {
    let mut f = File::open(path).map_err(|e| e.to_string())?;
    let mut head = [0u8; 12];
    f.read_exact(&mut head).map_err(|e| e.to_string())?;
    if &head[..8] != MAGIC { return Err("not a umapack".into()); }
    let len = u32::from_le_bytes(head[8..12].try_into().unwrap()) as usize;
    if len > 64 << 20 { return Err("header too large".into()); }
    let mut hb = vec![0u8; len];
    f.read_exact(&mut hb).map_err(|e| e.to_string())?;
    let h: Value = serde_json::from_slice(&hb).map_err(|e| e.to_string())?;
    let digest = h["digest"].as_str().filter(|d| d.len() == 64).ok_or("missing digest")?.to_owned();
    let (off, dlen) = (h["db"]["offset"].as_u64().ok_or("missing db")?, h["db"]["length"].as_u64().ok_or("missing db")?);
    let db = cache.join(format!("{digest}.db"));
    if std::fs::metadata(&db).map(|m| m.len()) .ok() != Some(dlen) {
        std::fs::create_dir_all(cache).map_err(|e| e.to_string())?;
        copy_range(&mut f, off, dlen, &db)?;
    }
    for e in h["files"].as_array().ok_or("missing files")? {
        let name = e["h"].as_str().ok_or("bad file entry")?.as_bytes();
        let Ok(key) = <[u8; 32]>::try_from(name) else { return Err("bad file hash".into()) };
        let (offset, len) = (e["offset"].as_u64().ok_or("bad offset")?, e["length"].as_u64().ok_or("bad length")?);
        files.entry(key).or_insert(Entry { pack: index, offset, len, ready: AtomicBool::new(false) });
    }
    Ok(Pack { path: path.to_owned(), digest, db })
}

fn copy_range(f: &mut File, offset: u64, len: u64, dst: &std::path::Path) -> Result<(), String> {
    f.seek(SeekFrom::Start(offset)).map_err(|e| e.to_string())?;
    let part = dst.with_extension("part");
    let mut out = File::create(&part).map_err(|e| e.to_string())?;
    let copied = std::io::copy(&mut Read::by_ref(f).take(len), &mut out).map_err(|e| e.to_string())?;
    drop(out);
    if copied != len { let _ = std::fs::remove_file(&part); return Err("pack is truncated".into()); }
    std::fs::rename(&part, dst).map_err(|e| e.to_string())
}

fn file_path(p: &Packs, h: &[u8; 32]) -> PathBuf {
    let name = std::str::from_utf8(h).unwrap_or("");
    p.cache.join("files").join(&name[..2]).join(name)
}

fn ensure_file(p: &Packs, h: &[u8; 32], e: &Entry) -> Option<PathBuf> {
    let path = file_path(p, h);
    if e.ready.load(Ordering::Acquire) { return Some(path); }
    let _guard = EXTRACT.lock().unwrap_or_else(|x| x.into_inner());
    if std::fs::metadata(&path).map(|m| m.len()).ok() != Some(e.len) {
        std::fs::create_dir_all(path.parent()?).ok()?;
        let mut f = File::open(&p.packs[e.pack].path).ok()?;
        if let Err(err) = copy_range(&mut f, e.offset, e.len, &path) {
            log(2, &format!("umapack: extract failed: {err}"));
            return None;
        }
    }
    e.ready.store(true, Ordering::Release);
    Some(path)
}

unsafe fn install_hooks() -> Result<(), String> {
    let conn = class(c"LibNative.Runtime.dll", c"LibNative.Sqlite3", c"Connection")?;
    let open = method(conn, c"OpenWritable", 7)?;
    let handle = method(conn, c"get_DBHandle", 0)?;
    let resolver = class(c"_Cyan.dll", c"Cyan.LocalFile", c"PathResolver")?;
    let local = method(resolver, c"GetLocalPath", 2)?;
    if export("libnative.dll", "sqlite3_exec", true) == 0 { return Err("sqlite3_exec not exported".into()); }
    DB_HANDLE.store(*(handle as *const usize), Ordering::Release);
    hook(open, hk_open_writable as *mut c_void, &OPEN_WRITABLE)?;
    hook(local, hk_local_path as *mut c_void, &LOCAL_PATH)?;
    if let Ok(ctrl) = class(c"umamusume.dll", c"Gallop", c"EventTimelineModelController") {
        if let Ok(m) = method(ctrl, c"LoadMotionClip", 4) {
            let _ = hook(m, hk_load_motion_clip as *mut c_void, &MOTION_CLIP);
        }
    }
    if let Ok(am) = class(c"Cute.Cri.Assembly.dll", c"Cute.Cri", c"AudioManager") {
        if let Ok(m) = method(am, c"GetSoundFilePath", 4) {
            let _ = hook(m, hk_sound_path as *mut c_void, &SOUND_PATH);
        }
    }
    if let Ok(am) = class(c"umamusume.dll", c"Gallop", c"AudioManager") {
        if let Ok(m) = method(am, c"IsExistAsset", 1) {
            let _ = hook(m, hk_sound_exist as *mut c_void, &SOUND_EXIST);
        }
    }
    Ok(())
}

fn pack_sound(name: &str) -> bool { name.contains("1091") || name.contains("1106") }

type SoundPathFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *mut *mut c_void, *mut *mut c_void, *const c_void);

unsafe extern "C" fn hk_sound_path(this: *mut c_void, sheet: *mut c_void, sub: *mut c_void, acb: *mut *mut c_void, awb: *mut *mut c_void, mi: *const c_void) {
    let f: SoundPathFn = std::mem::transmute(trampoline(&SOUND_PATH));
    f(this, sheet, sub, acb, awb, mi);
    if let (Some(p), Some(new)) = (PACKS.get(), API.get().and_then(|a| a.string_new)) {
        for out in [acb, awb] {
            if out.is_null() || (*out).is_null() { continue; }
            let Some(path) = il2cpp_text(*out) else { continue };
            let hash = path.rsplit(['\\', '/']).next().unwrap_or("");
            let Ok(key) = <[u8; 32]>::try_from(hash.as_bytes()) else { continue };
            if let Some(e) = p.files.get(&key) {
                if let Some(local) = ensure_file(p, &key, e) {
                    *out = new(cstr(&local.to_string_lossy()).as_ptr());
                }
            }
        }
    }
    let name = il2cpp_text(sheet).unwrap_or_default();
    if pack_sound(&name) {
        let get = |p: *mut *mut c_void| if p.is_null() { None } else { il2cpp_text(*p) };
        log(3, &format!("umapack: sound {name:?} sub={:?} acb={:?} awb={:?}", il2cpp_text(sub), get(acb), get(awb)));
    }
}

type SoundExistFn = unsafe extern "C" fn(*mut c_void, *const c_void) -> bool;

unsafe extern "C" fn hk_sound_exist(path: *mut c_void, mi: *const c_void) -> bool {
    let f: SoundExistFn = std::mem::transmute(trampoline(&SOUND_EXIST));
    let ok = f(path, mi);
    let name = il2cpp_text(path).unwrap_or_default();
    if pack_sound(&name) { log(3, &format!("umapack: sound exists {name:?} -> {ok}")); }
    ok
}

type OpenWritableFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *mut c_void, *mut c_void, bool, *mut c_void, i32, *const c_void) -> bool;

unsafe extern "C" fn hk_open_writable(this: *mut c_void, file: *mut c_void, journal: *mut c_void, sync: *mut c_void, locking: *mut c_void,
                                      shared: bool, key: *mut c_void, cipher: i32, mi: *const c_void) -> bool {
    let original: OpenWritableFn = std::mem::transmute(trampoline(&OPEN_WRITABLE));
    let name = il2cpp_text(file).unwrap_or_default();
    log(3, &format!("umapack: OpenWritable {name}"));
    let ok = original(this, file, journal, sync, locking, shared, key, cipher, mi);
    log(3, &format!("umapack: OpenWritable -> {ok}"));
    if ok && il2cpp_text(file).is_some_and(|p| p.replace('\\', "/").ends_with("/meta")) {
        let handle = DB_HANDLE.load(Ordering::Acquire);
        if handle != 0 {
            let get: unsafe extern "C" fn(*mut c_void, *const c_void) -> *mut c_void = std::mem::transmute(handle);
            let db = get(this, std::ptr::null());
            if !db.is_null() {
                log(3, "umapack: meta opened; syncing");
                let _ = std::panic::catch_unwind(|| sync_meta(db));
            }
        }
    }
    ok
}

type ExecFn = unsafe extern "C" fn(*mut c_void, *const c_char, Option<unsafe extern "C" fn(*mut c_void, i32, *mut *mut c_char, *mut *mut c_char) -> i32>, *mut c_void, *mut *mut c_char) -> i32;

fn exec(db: *mut c_void, sql: &str) -> Result<Vec<Vec<Option<String>>>, String> {
    unsafe extern "C" fn collect(out: *mut c_void, n: i32, vals: *mut *mut c_char, _: *mut *mut c_char) -> i32 {
        let rows = &mut *(out as *mut Vec<Vec<Option<String>>>);
        rows.push((0..n as usize).map(|i| {
            let v = *vals.add(i);
            (!v.is_null()).then(|| CStr::from_ptr(v).to_string_lossy().into_owned())
        }).collect());
        0
    }
    let f = export("libnative.dll", "sqlite3_exec", false);
    if f == 0 { return Err("sqlite3_exec missing".into()); }
    let f: ExecFn = unsafe { std::mem::transmute(f) };
    let mut rows: Vec<Vec<Option<String>>> = Vec::new();
    let mut err: *mut c_char = std::ptr::null_mut();
    let sql = cstr(sql);
    let rc = unsafe { f(db, sql.as_ptr(), Some(collect), &mut rows as *mut _ as *mut c_void, &mut err) };
    if rc != 0 {
        let msg = if err.is_null() { format!("sqlite error {rc}") } else { unsafe { CStr::from_ptr(err) }.to_string_lossy().into_owned() };
        if !err.is_null() {
            let free = export("libnative.dll", "sqlite3_free", false);
            if free != 0 { unsafe { std::mem::transmute::<usize, unsafe extern "C" fn(*mut c_void)>(free)(err as *mut c_void) } }
        }
        return Err(msg);
    }
    Ok(rows)
}

fn sql_text(s: &str) -> String { s.replace('\'', "''") }

fn sync_meta(db: *mut c_void) {
    let Some(p) = PACKS.get() else { return };
    let t = Instant::now();
    let marker = exec(db, "SELECT (SELECT v FROM i WHERE k='umaps'), (SELECT COUNT(*) FROM a WHERE i < 0)")
        .ok().and_then(|r| r.into_iter().next())
        .map(|r| format!("{}|{}", r[0].clone().unwrap_or_default(), r[1].clone().unwrap_or_default()));
    if let Some(m) = &marker {
        if let Some((stored, live)) = m.rsplit_once('|') {
            if stored.rsplit_once('|').is_some_and(|(fp, n)| fp == p.fingerprint && n == live) {
                let _ = exec(db, "UPDATE a SET s = 1 WHERE i < 0 AND s <> 1");
                status(format!("meta verified in {:.1} ms", t.elapsed().as_secs_f64() * 1000.0));
                return;
            }
        }
    }
    log(3, &format!("umapack: meta marker {marker:?}; rebuilding pack rows"));
    let mut sql = String::new();
    for (k, pack) in p.packs.iter().enumerate() {
        sql += &format!("ATTACH DATABASE '{}' AS umapack{k} KEY '';", sql_text(&pack.db.to_string_lossy()));
    }
    sql += "BEGIN IMMEDIATE;
        DELETE FROM main.r WHERE f < 0 OR t < 0;
        DELETE FROM main.a WHERE i < 0;
        CREATE TEMP TABLE umaps_new(rid INTEGER PRIMARY KEY, n TEXT NOT NULL UNIQUE, d, g, l, c, h, m, k, s, p, e);";
    for k in 0..p.packs.len() {
        sql += &format!("INSERT OR IGNORE INTO temp.umaps_new(n,d,g,l,c,h,m,k,s,p,e)
            SELECT n,d,g,l,c,h,m,k,1,p,e FROM umapack{k}.a WHERE n NOT IN (SELECT n FROM main.a);");
    }
    sql += "INSERT INTO main.a(i,n,d,g,l,c,h,m,k,s,p,e) SELECT -(1000000 + rid),n,d,g,l,c,h,m,k,s,p,e FROM temp.umaps_new;
        DROP TABLE temp.umaps_new;";
    for k in 0..p.packs.len() {
        sql += &format!("INSERT OR IGNORE INTO main.r(f,t,d)
            SELECT fa.i, ta.i, x.d FROM umapack{k}.rel x JOIN main.a fa ON fa.n = x.f JOIN main.a ta ON ta.n = x.t WHERE fa.i < 0;");
    }
    sql += &format!("INSERT OR REPLACE INTO main.i(k,v) SELECT 'umaps', '{}|' || (SELECT COUNT(*) FROM main.a WHERE i < 0); COMMIT;",
        sql_text(&p.fingerprint));
    let result = exec(db, &sql);
    if result.is_err() { let _ = exec(db, "ROLLBACK"); }
    for k in 0..p.packs.len() { let _ = exec(db, &format!("DETACH DATABASE umapack{k}")); }
    match result {
        Ok(_) => {
            let n = exec(db, "SELECT COUNT(*) FROM a WHERE i < 0").ok().and_then(|r| r.into_iter().next()).and_then(|r| r[0].clone());
            status(format!("meta synced: {} pack rows in {:.0} ms", n.unwrap_or_default(), t.elapsed().as_secs_f64() * 1000.0));
        }
        Err(e) => status(format!("meta sync failed: {e}")),
    }
}

type LoadMotionClipFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *mut c_void, *mut c_void, *const c_void) -> bool;

unsafe extern "C" fn hk_load_motion_clip(this: *mut c_void, key: *mut c_void, clip: *mut c_void, original: *mut c_void,
                                         info: *mut c_void, mi: *const c_void) -> bool {
    let f: LoadMotionClipFn = std::mem::transmute(trampoline(&MOTION_CLIP));
    let ok = f(this, key, clip, original, info, mi);
    if !ok {
        log(2, &format!("umapack: motion clip failed key={:?} clip={:?} original={:?}",
            il2cpp_text(key), il2cpp_text(clip), il2cpp_text(original)));
    }
    ok
}

type LocalPathFn = unsafe extern "C" fn(*mut c_void, i32, *mut c_void, *const c_void) -> *mut c_void;

unsafe extern "C" fn hk_local_path(this: *mut c_void, kind: i32, hname: *mut c_void, mi: *const c_void) -> *mut c_void {
    let original: LocalPathFn = std::mem::transmute(trampoline(&LOCAL_PATH));
    if let (Some(p), Some(api)) = (PACKS.get(), API.get()) {
        if let (false, Some(len), Some(chars)) = (hname.is_null(), api.string_length, api.string_chars) {
            if len(hname) == 32 {
                let c = chars(hname);
                if !c.is_null() {
                    let mut key = [0u8; 32];
                    let mut ascii = true;
                    for (i, k) in key.iter_mut().enumerate() {
                        let u = *c.add(i);
                        if u > 0x7f { ascii = false; break; }
                        *k = u as u8;
                    }
                    if ascii {
                        if let Some(e) = p.files.get(&key) {
                            if let (Some(path), Some(new)) = (ensure_file(p, &key, e), api.string_new) {
                                return new(cstr(&path.to_string_lossy()).as_ptr());
                            }
                        }
                    }
                }
            }
        }
    }
    original(this, kind, hname, mi)
}

pub fn menu(u: &Ui<'_>) {
    let text = STATUS.lock().unwrap_or_else(|e| e.into_inner()).clone();
    if text.is_empty() { return; }
    u.separator();
    u.heading("Trainee packs");
    u.small(&text);
}
