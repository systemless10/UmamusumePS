
use std::ffi::{c_char, c_void, CStr, CString};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Mutex, OnceLock, RwLock};
use std::time::{Duration, Instant};
mod custom_support;
mod umapack;

const OFFICIAL_ORIGINS: [&str; 2] = ["https://api.games.umamusume.com", "https://api.games.umamusume.jp"];
const JP_HOST: &str = "api.games.umamusume.jp";
const DEFAULT_ARM_PORT: u16 = 8090;
const RECHECK: Duration = Duration::from_secs(3);
const CONFIG_NAME: &str = "redirect.cfg";

type GetApiFn = unsafe extern "C" fn(name: *const c_char) -> *mut c_void;
type LogFn = unsafe extern "C" fn(i32, *const c_char, *const c_char);
type InstanceFn = unsafe extern "C" fn() -> *const c_void;
type GetInterceptorFn = unsafe extern "C" fn(*const c_void) -> *const c_void;
type HookFn = unsafe extern "C" fn(*const c_void, *mut c_void, *mut c_void) -> *mut c_void;
type GetImageFn = unsafe extern "C" fn(*const c_char) -> *const c_void;
type GetClassFn = unsafe extern "C" fn(*const c_void, *const c_char, *const c_char) -> *mut c_void;
type GetMethodAddrFn = unsafe extern "C" fn(*mut c_void, *const c_char, i32) -> *mut c_void;
type OnGameInitFn = unsafe extern "C" fn(Option<unsafe extern "C" fn(*mut c_void)>, *mut c_void) -> bool;
type StringNewFn = unsafe extern "C" fn(*const c_char) -> *mut c_void;
type StringCharsFn = unsafe extern "C" fn(*mut c_void) -> *mut u16;
type StringLengthFn = unsafe extern "C" fn(*mut c_void) -> i32;
type GetBaseDirFn = unsafe extern "C" fn() -> *const c_char;
type SectionCb = extern "C" fn(*mut c_void, *mut c_void);
type RegisterSectionFn = unsafe extern "C" fn(Option<SectionCb>, *mut c_void) -> bool;
type NotifyFn = unsafe extern "C" fn(*const c_char) -> bool;
type UiTextFn = unsafe extern "C" fn(*mut c_void, *const c_char) -> bool;
type UiSeparatorFn = unsafe extern "C" fn(*mut c_void) -> bool;
type UiCheckboxFn = unsafe extern "C" fn(*mut c_void, *const c_char, *mut bool) -> bool;
type UiTextEditFn = unsafe extern "C" fn(*mut c_void, *mut c_char, usize) -> bool;
type UiColoredFn = unsafe extern "C" fn(*mut c_void, u8, u8, u8, u8, *const c_char) -> bool;
type ResolveSymbolFn = unsafe extern "C" fn(*const c_char) -> *mut c_void;
type GetFieldFn = unsafe extern "C" fn(*mut c_void, *const c_char) -> *mut c_void;
type SetFieldFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *const c_void);

struct Api {
    log: Option<LogFn>,
    instance: Option<InstanceFn>,
    get_interceptor: Option<GetInterceptorFn>,
    hook: Option<HookFn>,
    get_image: Option<GetImageFn>,
    get_class: Option<GetClassFn>,
    get_method_addr: Option<GetMethodAddrFn>,
    on_game_init: Option<OnGameInitFn>,
    string_new: Option<StringNewFn>,
    string_chars: Option<StringCharsFn>,
    string_length: Option<StringLengthFn>,
    base_dir: Option<GetBaseDirFn>,
    register_section: Option<RegisterSectionFn>,
    notify: Option<NotifyFn>,
    heading: Option<UiTextFn>,
    label: Option<UiTextFn>,
    small: Option<UiTextFn>,
    button: Option<UiTextFn>,
    separator: Option<UiSeparatorFn>,
    checkbox: Option<UiCheckboxFn>,
    text_edit: Option<UiTextEditFn>,
    colored: Option<UiColoredFn>,
    resolve_symbol: Option<ResolveSymbolFn>,
    get_field: Option<GetFieldFn>,
    set_field: Option<SetFieldFn>,
}

static API: OnceLock<Api> = OnceLock::new();

unsafe fn resolve<T: Copy>(get: GetApiFn, name: &str) -> Option<T> {
    let n = CString::new(name).ok()?;
    let p = get(n.as_ptr());
    if p.is_null() {
        None
    } else {
        Some(std::mem::transmute_copy::<*mut c_void, T>(&p))
    }
}

fn cstr(s: &str) -> CString {
    CString::new(s.replace('\0', " ")).unwrap_or_default()
}

fn log(level: i32, msg: &str) {
    let Some(api) = API.get() else { return };
    let Some(f) = api.log else { return };
    let (t, m) = (cstr("redirect"), cstr(msg));
    unsafe { f(level, t.as_ptr(), m.as_ptr()) }
}

fn notify(msg: &str) {
    if let Some(f) = API.get().and_then(|a| a.notify) {
        let m = cstr(msg);
        unsafe { f(m.as_ptr()) };
    }
}

#[no_mangle]
pub extern "C" fn hachimi_init_v3(get_api: GetApiFn, version: i32) -> i32 {
    if version < 3 {
        return 0;
    }
    let api = unsafe {
        Api {
            log: resolve(get_api, "log"),
            instance: resolve(get_api, "hachimi_instance"),
            get_interceptor: resolve(get_api, "hachimi_get_interceptor"),
            hook: resolve(get_api, "interceptor_hook"),
            get_image: resolve(get_api, "il2cpp_get_assembly_image"),
            get_class: resolve(get_api, "il2cpp_get_class"),
            get_method_addr: resolve(get_api, "il2cpp_get_method_addr"),
            on_game_init: resolve(get_api, "hachimi_register_on_game_initialized"),
            string_new: resolve(get_api, "il2cpp_string_new"),
            string_chars: resolve(get_api, "il2cpp_string_chars"),
            string_length: resolve(get_api, "il2cpp_string_length"),
            base_dir: resolve(get_api, "hachimi_get_base_dir"),
            register_section: resolve(get_api, "gui_register_menu_section"),
            notify: resolve(get_api, "gui_show_notification"),
            heading: resolve(get_api, "gui_ui_heading"),
            label: resolve(get_api, "gui_ui_label"),
            small: resolve(get_api, "gui_ui_small"),
            button: resolve(get_api, "gui_ui_button"),
            separator: resolve(get_api, "gui_ui_separator"),
            checkbox: resolve(get_api, "gui_ui_checkbox"),
            text_edit: resolve(get_api, "gui_ui_text_edit_singleline"),
            colored: resolve(get_api, "gui_ui_colored_label"),
            resolve_symbol: resolve(get_api, "il2cpp_resolve_symbol"),
            get_field: resolve(get_api, "il2cpp_get_field_from_name"),
            set_field: resolve(get_api, "il2cpp_set_field_value"),
        }
    };
    let _ = API.set(api);
    load_config();
    if custom_support::enabled() {
        custom_support::install();
    }
    let _ = std::panic::catch_unwind(umapack::install);

    let api = API.get().unwrap();
    if let Some(reg) = api.register_section {
        unsafe { reg(Some(menu_section), std::ptr::null_mut()) };
    }
    install_cert_hook();
    if region() == Region::Jp {
        spawn_seal_installer();
    }
    try_install_post(false);
    let registered = api.on_game_init.map(|f| unsafe { f(Some(on_game_initialized), std::ptr::null_mut()) });
    if registered != Some(true) {
        try_install_post(true);
        install_managed_hooks();
    }
    let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
    log(
        3,
        &format!(
            "loaded ({:?} client); redirect {} -> {}",
            region(),
            if cfg.enabled { "ON" } else { "off" },
            cfg.server
        ),
    );
    1
}

unsafe extern "C" fn on_game_initialized(_: *mut c_void) {
    let _ = std::panic::catch_unwind(|| try_install_post(true));
    install_managed_hooks();
}

static MANAGED_HOOKS: AtomicBool = AtomicBool::new(false);

fn install_managed_hooks() {
    if MANAGED_HOOKS.swap(true, Ordering::AcqRel) {
        return;
    }
    let _ = std::panic::catch_unwind(try_install_censor);
    if BIG_FIELDS.load(Ordering::Acquire) {
        let _ = std::panic::catch_unwind(try_install_big_fields);
    }
    if custom_support::enabled() {
        custom_support::install();
    }
    if CLIENT_LOG.load(Ordering::Acquire) {
        let _ = std::panic::catch_unwind(|| set_client_log(true));
        dump_classes();
    }
}

#[link(name = "kernel32")]
extern "system" {
    fn GetModuleHandleW(name: *const u16) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const c_char) -> *mut c_void;
    fn LoadLibraryW(name: *const u16) -> *mut c_void;
    fn GetModuleFileNameW(module: *mut c_void, buf: *mut u16, size: u32) -> u32;
    fn VirtualQuery(addr: *const c_void, info: *mut MemoryBasicInformation, len: usize) -> usize;
}

#[repr(C)]
#[derive(Default)]
struct MemoryBasicInformation {
    base: usize,
    allocation_base: usize,
    allocation_protect: u32,
    partition_id: u16,
    region_size: usize,
    state: u32,
    protect: u32,
    kind: u32,
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn export(module: &str, symbol: &str, load: bool) -> usize {
    let w = wide(module);
    let Ok(sym) = CString::new(symbol) else { return 0 };
    unsafe {
        let mut h = GetModuleHandleW(w.as_ptr());
        if h.is_null() && load {
            h = LoadLibraryW(w.as_ptr());
        }
        if h.is_null() {
            return 0;
        }
        GetProcAddress(h, sym.as_ptr()) as usize
    }
}

fn writable(p: *const u8, len: usize) -> bool {
    if p.is_null() {
        return false;
    }
    [p, unsafe { p.add(len.saturating_sub(1)) }].iter().all(|&a| {
        let mut mbi = MemoryBasicInformation::default();
        let n = unsafe { VirtualQuery(a as *const c_void, &mut mbi, std::mem::size_of::<MemoryBasicInformation>()) };
        n != 0 && mbi.state == 0x1000 && mbi.protect & (0x04 | 0x08 | 0x40 | 0x80) != 0 && mbi.protect & 0x100 == 0
    })
}

fn interceptor_hook(addr: usize, detour: *mut c_void) -> usize {
    let Some(api) = API.get() else { return 0 };
    let (Some(inst), Some(get_it), Some(hook)) = (api.instance, api.get_interceptor, api.hook) else {
        return 0;
    };
    unsafe {
        let it = get_it(inst());
        if it.is_null() {
            return 0;
        }
        hook(it, addr as *mut c_void, detour) as usize
    }
}

fn trampoline(slot: &AtomicUsize) -> usize {
    loop {
        let t = slot.load(Ordering::Acquire);
        if t != 0 {
            return t;
        }
        std::hint::spin_loop();
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Region {
    Global,
    Jp,
}

fn region() -> Region {
    static R: OnceLock<Region> = OnceLock::new();
    *R.get_or_init(|| {
        let mut buf = [0u16; 1024];
        let n = unsafe { GetModuleFileNameW(std::ptr::null_mut(), buf.as_mut_ptr(), buf.len() as u32) } as usize;
        let exe = String::from_utf16_lossy(&buf[..n.min(buf.len())]).to_ascii_lowercase();
        if exe.contains("_jpn") {
            Region::Jp
        } else {
            Region::Global
        }
    })
}

fn default_server() -> &'static str {
    match region() {
        Region::Global => "https://127.0.0.1",
        Region::Jp => "https://127.0.0.2",
    }
}

struct Config {
    enabled: bool,
    server: String,
    host: String,
}

static CONFIG: RwLock<Config> = RwLock::new(Config { enabled: false, server: String::new(), host: String::new() });
static GENERATION: AtomicU64 = AtomicU64::new(0);
static REDIRECTED: AtomicU64 = AtomicU64::new(0);
static CENSOR: AtomicBool = AtomicBool::new(true);
static FUTURE_CONTENT: AtomicBool = AtomicBool::new(false);
static CENSORED: AtomicU64 = AtomicU64::new(0);
static CERTS_ACCEPTED: AtomicU64 = AtomicU64::new(0);
static CAPTURE: AtomicBool = AtomicBool::new(false);

fn normalize(input: &str) -> Result<(String, String), String> {
    let s = input.trim().trim_end_matches('/');
    if s.is_empty() {
        return Err("enter a server address".into());
    }
    let lower = s.to_ascii_lowercase();
    if lower.starts_with("http://") {
        return Err("this client only allows https:// (Unity blocks plain http)".into());
    }
    let rest = if lower.starts_with("https://") { &s[8..] } else if lower.contains("://") {
        return Err("only https:// addresses work".into());
    } else {
        s
    };
    let authority = rest.split('/').next().unwrap_or("");
    let host = if let Some(v6) = authority.strip_prefix('[') {
        v6.split(']').next().unwrap_or("")
    } else {
        authority.split(':').next().unwrap_or("")
    };
    if host.is_empty() || host.contains(char::is_whitespace) {
        return Err("no host in that address".into());
    }
    if let Some((_, port)) = authority.rsplit_once(':').filter(|_| !authority.ends_with(']')) {
        if !authority.starts_with('[') && port.parse::<u16>().is_err() {
            return Err(format!("bad port \"{port}\""));
        }
    }
    Ok((format!("https://{rest}"), host.to_ascii_lowercase()))
}

fn config_path() -> Option<PathBuf> {
    let f = API.get()?.base_dir?;
    let p = unsafe { f() };
    if p.is_null() {
        return None;
    }
    let dir = unsafe { CStr::from_ptr(p) }.to_str().ok()?;
    Some(PathBuf::from(dir).join(CONFIG_NAME))
}

fn load_config() {
    let mut enabled = false;
    let mut server = default_server().to_string();
    if let Some(text) = config_path().and_then(|p| std::fs::read_to_string(p).ok()) {
        for line in text.lines() {
            match line.split_once('=').map(|(k, v)| (k.trim(), v.trim())) {
                Some(("enabled", v)) => enabled = matches!(v, "1" | "true" | "yes" | "on"),
                Some(("server", v)) if !v.is_empty() => server = v.to_string(),
                Some(("future_content", v)) => FUTURE_CONTENT.store(matches!(v, "1" | "true" | "yes" | "on"), Ordering::Release),
                Some(("capture", v)) => CAPTURE.store(matches!(v, "1" | "true" | "yes" | "on"), Ordering::Release),
                Some(("client_log", v)) => CLIENT_LOG.store(matches!(v, "1" | "true" | "yes" | "on"), Ordering::Release),
                Some(("big_fields", v)) => BIG_FIELDS.store(!matches!(v, "0" | "false" | "no" | "off"), Ordering::Release),
                Some(("custom_support", v)) => custom_support::ENABLED.store(matches!(v, "1" | "true" | "yes" | "on"), Ordering::Release),
                Some(("server_events", v)) => custom_support::SERVER_EVENTS.store(matches!(v, "1" | "true" | "yes" | "on"), Ordering::Release),
                Some(("unreleased_trainees", v)) => custom_support::UNRELEASED.store(matches!(v, "1" | "true" | "yes" | "on"), Ordering::Release),
                _ => {}
            }
        }
    }
    let (server, host, enabled) = match normalize(&server) {
        Ok((s, h)) => (s, h, enabled),
        Err(e) => {
            log(2, &format!("{CONFIG_NAME}: server \"{server}\" rejected ({e}); redirect off"));
            let (s, h) = normalize(default_server()).unwrap();
            (s, h, false)
        }
    };
    set_config(enabled, server, host, false);
}

fn set_config(enabled: bool, server: String, host: String, save: bool) {
    if custom_support::LOADED.load(Ordering::Acquire) {
        let old = CONFIG.read().unwrap_or_else(|e| e.into_inner());
        if old.enabled != enabled || old.server != server {
            notify("A custom master is loaded. Restart before changing servers.");
            return;
        }
    }
    {
        let mut cfg = CONFIG.write().unwrap_or_else(|e| e.into_inner());
        *cfg = Config { enabled, server, host };
        GENERATION.fetch_add(1, Ordering::AcqRel);
        if save {
            save_config(&cfg);
        }
    }
    let mut ui = UI.lock().unwrap_or_else(|e| e.into_inner());
    ui.reset_from_config();
}

fn save_config(cfg: &Config) {
    let Some(p) = config_path() else { return };
    let text = format!(
        "# Server redirect (Hachimi plugin hachimi_redirect)\nenabled={}\nserver={}\nfuture_content={}\ncapture={}\nclient_log={}\nbig_fields={}\ncustom_support={}\nserver_events={}\nunreleased_trainees={}\n",
        cfg.enabled,
        cfg.server,
        FUTURE_CONTENT.load(Ordering::Acquire),
        CAPTURE.load(Ordering::Acquire),
        CLIENT_LOG.load(Ordering::Acquire),
        BIG_FIELDS.load(Ordering::Acquire),
        custom_support::ENABLED.load(Ordering::Acquire),
        custom_support::SERVER_EVENTS.load(Ordering::Acquire),
        custom_support::UNRELEASED.load(Ordering::Acquire),
    );
    if let Err(e) = std::fs::write(&p, text) {
        log(1, &format!("could not save {}: {e}", p.display()));
    }
}

type PostFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *mut c_void, *const c_void);
static POST_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static POST_INSTALLED: OnceLock<bool> = OnceLock::new();

unsafe extern "C" fn hk_post(this: *mut c_void, url: *mut c_void, data: *mut c_void, headers: *mut c_void, mi: *const c_void) {
    if !MANAGED_HOOKS.load(Ordering::Acquire) {
        install_managed_hooks();
    }
    let _ = std::panic::catch_unwind(|| {
        if capturing() {
            if let Some(u) = il2cpp_text(url) {
                capture_write("http_txn", &format!(",\"method\":\"POST\",\"url\":{}", json_str(&u)));
            }
        }
    });
    let url = std::panic::catch_unwind(|| rewrite(url)).ok().flatten().unwrap_or(url);
    if il2cpp_text(url).is_some_and(|u| {
        let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
        cfg.enabled && u.starts_with(&(cfg.server.clone() + "/"))
    }) {
        let _ = std::panic::catch_unwind(|| custom_support::add_header(headers));
    }
    std::mem::transmute::<usize, PostFn>(trampoline(&POST_TRAMPOLINE))(this, url, data, headers, mi)
}

fn il2cpp_text(s: *mut c_void) -> Option<String> {
    if s.is_null() {
        return None;
    }
    let api = API.get()?;
    let (chars, length) = (api.string_chars?, api.string_length?);
    unsafe {
        let n = length(s);
        let p = chars(s);
        if p.is_null() || n <= 0 {
            return None;
        }
        Some(String::from_utf16_lossy(std::slice::from_raw_parts(p, n as usize)))
    }
}

fn rewrite(url: *mut c_void) -> Option<*mut c_void> {
    if url.is_null() {
        return None;
    }
    let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
    if !cfg.enabled {
        return None;
    }
    let new = API.get()?.string_new?;
    let text = il2cpp_text(url)?;
    let lower = text.to_ascii_lowercase();
    let origin = OFFICIAL_ORIGINS
        .iter()
        .find(|o| lower.starts_with(*o) && matches!(lower.as_bytes().get(o.len()), None | Some(b'/') | Some(b'?')))?;
    let target = format!("{}{}", cfg.server, &text[origin.len()..]);
    if REDIRECTED.fetch_add(1, Ordering::Relaxed) == 0 {
        log(3, &format!("first request redirected: {text} -> {target}"));
    }
    let c = CString::new(target).ok()?;
    let s = unsafe { new(c.as_ptr()) };
    (!s.is_null()).then_some(s)
}

fn try_install_post(final_attempt: bool) -> bool {
    if POST_INSTALLED.get().is_some() {
        return true;
    }
    let Some(api) = API.get() else { return false };
    let (Some(get_image), Some(get_class), Some(get_addr)) = (api.get_image, api.get_class, api.get_method_addr) else {
        log(1, "Hachimi il2cpp API unavailable; traffic will NOT be redirected");
        let _ = POST_INSTALLED.set(false);
        return true;
    };
    unsafe {
        let image = get_image(c"Cute.Http.Assembly.dll".as_ptr());
        let class = if image.is_null() {
            std::ptr::null_mut()
        } else {
            get_class(image, c"Cute.Http".as_ptr(), c"WWWRequest".as_ptr())
        };
        if class.is_null() {
            if final_attempt {
                log(1, "Cute.Http.WWWRequest not found; traffic will NOT be redirected");
                let _ = POST_INSTALLED.set(false);
            }
            return false;
        }
        let addr = get_addr(class, c"Post".as_ptr(), 3) as usize;
        if addr == 0 {
            log(1, "WWWRequest.Post(url, data, headers) not found (client changed?); traffic will NOT be redirected");
            let _ = POST_INSTALLED.set(false);
            return true;
        }
        let tramp = interceptor_hook(addr, hk_post as *mut c_void);
        if tramp == 0 {
            log(1, "failed to hook WWWRequest.Post; traffic will NOT be redirected");
            let _ = POST_INSTALLED.set(false);
            return true;
        }
        POST_TRAMPOLINE.store(tramp, Ordering::Release);
    }
    let _ = POST_INSTALLED.set(true);
    log(3, "hooked Cute.Http.WWWRequest.Post");
    true
}

const CENSORED_STRINGS: [&str; 10] = [
    "device_id",
    "device_name",
    "graphics_device_name",
    "platform_os_version",
    "ip_address",
    "locale",
    "carrier",
    "steam_id",
    "steam_session_ticket",
    "steam_session_auth_ticket",
];

type SetupFn = unsafe extern "C" fn(*mut c_void, *const c_void);
static SETUP_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static CENSOR_INSTALLED: OnceLock<bool> = OnceLock::new();

struct CensorFields {
    request_common: usize,
    strings: Vec<usize>,
    keychain: usize,
}

static FIELDS: OnceLock<CensorFields> = OnceLock::new();

unsafe extern "C" fn hk_setup_request_common(req: *mut c_void, mi: *const c_void) {
    std::mem::transmute::<usize, SetupFn>(trampoline(&SETUP_TRAMPOLINE))(req, mi);
    let _ = std::panic::catch_unwind(|| censor(req));
}

fn censor(req: *mut c_void) {
    if req.is_null() || !CENSOR.load(Ordering::Acquire) {
        return;
    }
    if !CONFIG.read().unwrap_or_else(|e| e.into_inner()).enabled {
        return;
    }
    let (Some(f), Some(api)) = (FIELDS.get(), API.get()) else { return };
    let (Some(set), Some(new_str)) = (api.set_field, api.string_new) else { return };
    let Some(is_subclass) = il2cpp::<unsafe extern "C" fn(*mut c_void, *mut c_void, bool) -> bool>("il2cpp_class_is_subclass_of")
    else {
        return;
    };
    unsafe {
        let klass = *(req as *const *mut c_void);
        if klass as usize != f.request_common && !is_subclass(klass, f.request_common as *mut c_void, false) {
            return;
        }
        let empty = new_str(c"".as_ptr());
        for &field in &f.strings {
            set(req, field as *mut c_void, empty as *const c_void);
        }
        if f.keychain != 0 {
            let zero: i64 = 0;
            set(req, f.keychain as *mut c_void, &zero as *const i64 as *const c_void);
        }
    }
    if CENSORED.fetch_add(1, Ordering::Relaxed) == 0 {
        log(3, "censoring device and Steam identifiers on redirected requests");
    }
}

fn il2cpp<T: Copy>(name: &str) -> Option<T> {
    let f = API.get()?.resolve_symbol?;
    let n = CString::new(name).ok()?;
    let p = unsafe { f(n.as_ptr()) };
    if p.is_null() {
        None
    } else {
        Some(unsafe { std::mem::transmute_copy::<*mut c_void, T>(&p) })
    }
}

fn try_install_censor() {
    if CENSOR_INSTALLED.get().is_some() {
        return;
    }
    let ok = unsafe { install_censor() };
    if let Err(e) = &ok {
        log(1, &format!("censor hook not installed ({e}); identifiers will NOT be hidden"));
    }
    let _ = CENSOR_INSTALLED.set(ok.is_ok());
}

unsafe fn install_censor() -> Result<(), String> {
    type DomainGet = unsafe extern "C" fn() -> *mut c_void;
    type ThreadAttach = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type MethodFromName = unsafe extern "C" fn(*mut c_void, *const c_char, i32) -> *const c_void;
    type MethodGetObject = unsafe extern "C" fn(*const c_void, *mut c_void) -> *mut c_void;
    type ClassGetType = unsafe extern "C" fn(*mut c_void) -> *const c_void;
    type TypeGetObject = unsafe extern "C" fn(*const c_void) -> *mut c_void;
    type ArrayNew = unsafe extern "C" fn(*mut c_void, usize) -> *mut c_void;
    type RuntimeInvoke = unsafe extern "C" fn(*const c_void, *mut c_void, *mut *mut c_void, *mut *mut c_void) -> *mut c_void;
    type FromReflection = unsafe extern "C" fn(*mut c_void) -> *const c_void;

    let api = API.get().ok_or("no API")?;
    let (get_image, get_class, get_field) =
        (api.get_image.ok_or("no il2cpp API")?, api.get_class.ok_or("no il2cpp API")?, api.get_field.ok_or("no field API")?);
    api.set_field.ok_or("no field API")?;
    let missing = |n: &str| format!("il2cpp export {n} unavailable");
    let domain_get: DomainGet = il2cpp("il2cpp_domain_get").ok_or_else(|| missing("domain_get"))?;
    let attach: ThreadAttach = il2cpp("il2cpp_thread_attach").ok_or_else(|| missing("thread_attach"))?;
    let from_name: MethodFromName =
        il2cpp("il2cpp_class_get_method_from_name").ok_or_else(|| missing("class_get_method_from_name"))?;
    let get_object: MethodGetObject = il2cpp("il2cpp_method_get_object").ok_or_else(|| missing("method_get_object"))?;
    let class_type: ClassGetType = il2cpp("il2cpp_class_get_type").ok_or_else(|| missing("class_get_type"))?;
    let type_object: TypeGetObject = il2cpp("il2cpp_type_get_object").ok_or_else(|| missing("type_get_object"))?;
    let array_new: ArrayNew = il2cpp("il2cpp_array_new").ok_or_else(|| missing("array_new"))?;
    let invoke: RuntimeInvoke = il2cpp("il2cpp_runtime_invoke").ok_or_else(|| missing("runtime_invoke"))?;
    let from_reflection: FromReflection =
        il2cpp("il2cpp_method_get_from_reflection").ok_or_else(|| missing("method_get_from_reflection"))?;
    il2cpp::<unsafe extern "C" fn(*mut c_void, *mut c_void, bool) -> bool>("il2cpp_class_is_subclass_of")
        .ok_or_else(|| missing("class_is_subclass_of"))?;

    attach(domain_get());

    let corlib = get_image(c"mscorlib.dll".as_ptr());
    if corlib.is_null() {
        return Err("mscorlib.dll image not found".into());
    }
    let find = |name: &CStr| {
        [c"umamusume.dll", c"umamusume.Http.dll"]
            .iter()
            .map(|image| get_image(image.as_ptr()))
            .filter(|image| !image.is_null())
            .map(|image| get_class(image, c"Gallop".as_ptr(), name.as_ptr()))
            .find(|class| !class.is_null())
            .unwrap_or(std::ptr::null_mut())
    };
    let helper = find(c"HttpHelper");
    let request_common = find(c"RequestCommon");
    let type_class = get_class(corlib, c"System".as_ptr(), c"Type".as_ptr());
    if helper.is_null() || request_common.is_null() || type_class.is_null() {
        return Err(format!(
            "{} not found in umamusume.dll / umamusume.Http.dll",
            if helper.is_null() { "Gallop.HttpHelper" } else { "Gallop.RequestCommon" }
        ));
    }

    let mut strings = Vec::new();
    for name in CENSORED_STRINGS {
        let n = cstr(name);
        let f = get_field(request_common, n.as_ptr());
        if !f.is_null() {
            strings.push(f as usize);
        }
    }
    let keychain = get_field(request_common, c"keychain".as_ptr()) as usize;
    if strings.is_empty() {
        return Err("none of the RequestCommon fields exist (client changed?)".into());
    }

    let generic = from_name(helper, c"SetupRequestCommon".as_ptr(), 1);
    if generic.is_null() {
        return Err("HttpHelper.SetupRequestCommon not found".into());
    }
    let refl = get_object(generic, helper);
    if refl.is_null() {
        return Err("no reflection object for SetupRequestCommon".into());
    }
    let refl_class = *(refl as *const *mut c_void);
    let make = from_name(refl_class, c"MakeGenericMethod".as_ptr(), 1);
    if make.is_null() {
        return Err("MethodInfo.MakeGenericMethod not found".into());
    }
    let args_arr = array_new(type_class, 1);
    if args_arr.is_null() {
        return Err("could not allocate Type[]".into());
    }
    *((args_arr as *mut u8).add(0x20) as *mut *mut c_void) = type_object(class_type(request_common));
    let mut params = [args_arr];
    let mut exc: *mut c_void = std::ptr::null_mut();
    let inflated = invoke(make, refl, params.as_mut_ptr(), &mut exc);
    if !exc.is_null() || inflated.is_null() {
        return Err("MakeGenericMethod(RequestCommon) threw".into());
    }
    let method = from_reflection(inflated);
    let addr = if method.is_null() { 0 } else { *(method as *const usize) };
    if addr == 0 {
        return Err("SetupRequestCommon<object> has no compiled body".into());
    }

    let _ = FIELDS.set(CensorFields { request_common: request_common as usize, strings, keychain });
    let tramp = interceptor_hook(addr, hk_setup_request_common as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook SetupRequestCommon".into());
    }
    SETUP_TRAMPOLINE.store(tramp, Ordering::Release);
    log(3, &format!("hooked HttpHelper.SetupRequestCommon<object> at {addr:#x}"));
    Ok(())
}

type VerifyPolicyFn = unsafe extern "system" fn(usize, *const u8, *const u8, *mut u8) -> i32;
static VERIFY_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static CERT_HOOKED: OnceLock<bool> = OnceLock::new();
const CERT_CHAIN_POLICY_SSL: usize = 4;

unsafe extern "system" fn hk_verify_policy(oid: usize, chain: *const u8, para: *const u8, status: *mut u8) -> i32 {
    let ok = std::mem::transmute::<usize, VerifyPolicyFn>(trampoline(&VERIFY_TRAMPOLINE))(oid, chain, para, status);
    if oid != CERT_CHAIN_POLICY_SSL || chain.is_null() || para.is_null() || status.is_null() {
        return ok;
    }
    match std::panic::catch_unwind(|| accept_our_server(chain, para, status)) {
        Ok(true) => 1,
        _ => ok,
    }
}

fn accept_our_server(chain: *const u8, para: *const u8, status: *mut u8) -> bool {
    unsafe {
        let extra = *(para.add(8) as *const *const u8);
        if extra.is_null() {
            return false;
        }
        let name = *(extra.add(0x10) as *const *const u16);
        if name.is_null() {
            return false;
        }
        let mut len = 0;
        while len < 512 && *name.add(len) != 0 {
            len += 1;
        }
        let server_name = String::from_utf16_lossy(std::slice::from_raw_parts(name, len)).to_ascii_lowercase();
        {
            let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
            if !cfg.enabled || server_name != cfg.host {
                return false;
            }
        }
        if *(status.add(4) as *const u32) != 0 {
            *(status.add(4) as *mut u32) = 0;
            *(status.add(8) as *mut i32) = -1;
            *(status.add(0xC) as *mut i32) = -1;
            clear_chain_errors(chain);
            if CERTS_ACCEPTED.fetch_add(1, Ordering::Relaxed) == 0 {
                log(3, &format!("accepted the certificate of {server_name} (the configured server)"));
            }
        }
        true
    }
}

unsafe fn clear_chain_errors(ctx: *const u8) {
    let clear = |p: *const u8| {
        if writable(p, 4) {
            *(p as *mut u32) = 0;
        }
    };
    clear(ctx.add(4));
    let n_chains = *(ctx.add(0xC) as *const u32) as usize;
    let chains = *(ctx.add(0x10) as *const *const *const u8);
    if chains.is_null() {
        return;
    }
    for i in 0..n_chains.min(16) {
        let simple = *chains.add(i);
        if simple.is_null() {
            continue;
        }
        clear(simple.add(4));
        let n_elems = *(simple.add(0xC) as *const u32) as usize;
        let elems = *(simple.add(0x10) as *const *const u8);
        if elems.is_null() {
            continue;
        }
        for j in 0..n_elems.min(16) {
            let el = *(elems as *const *const u8).add(j);
            if !el.is_null() {
                clear(el.add(0x10));
            }
        }
    }
}

fn install_cert_hook() {
    let addr = export("crypt32.dll", "CertVerifyCertificateChainPolicy", true);
    if addr == 0 {
        log(1, "crypt32!CertVerifyCertificateChainPolicy not found; the server's certificate must be trusted by the system");
        let _ = CERT_HOOKED.set(false);
        return;
    }
    let tramp = interceptor_hook(addr, hk_verify_policy as *mut c_void);
    if tramp == 0 {
        log(1, "failed to hook CertVerifyCertificateChainPolicy; the server's certificate must be trusted by the system");
        let _ = CERT_HOOKED.set(false);
        return;
    }
    VERIFY_TRAMPOLINE.store(tramp, Ordering::Release);
    let _ = CERT_HOOKED.set(true);
    log(3, "hooked crypt32!CertVerifyCertificateChainPolicy (only the configured server's name is affected)");
}

type Lz4Fn = unsafe extern "C" fn(*const u8, *mut u8, i32, i32, usize, usize) -> i32;
static LZ4_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static SEAL_STATE: Mutex<&'static str> = Mutex::new("waiting for libnative.dll");

static LZ4_DEC_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);

unsafe extern "C" fn hk_compress(src: *const u8, dst: *mut u8, n: i32, cap: i32, a5: usize, a6: usize) -> i32 {
    let _ = std::panic::catch_unwind(|| capture_request(src, n));
    let _ = std::panic::catch_unwind(|| patch_envelope(src as *mut u8, n));
    std::mem::transmute::<usize, Lz4Fn>(trampoline(&LZ4_TRAMPOLINE))(src, dst, n, cap, a5, a6)
}

unsafe extern "C" fn hk_decompress(src: *const u8, dst: *mut u8, n: i32, cap: i32, a5: usize, a6: usize) -> i32 {
    let ret = std::mem::transmute::<usize, Lz4Fn>(trampoline(&LZ4_DEC_TRAMPOLINE))(src, dst, n, cap, a5, a6);
    if ret > 0 {
        let _ = std::panic::catch_unwind(|| capture_reply(dst, ret));
    }
    ret
}

fn envelope_header(src: *const u8, n: i32) -> Option<usize> {
    if src.is_null() || n <= 0 {
        return None;
    }
    let header = match unsafe { *src } {
        0xA6 => 0xAA,
        0x74 => 0x78,
        _ => return None,
    };
    let env = unsafe { std::slice::from_raw_parts(src, (n as usize).min(header)) };
    ((n as usize) > header && env[1..4] == [0, 0, 0]).then_some(header)
}

fn patch_envelope(src: *mut u8, n: i32) {
    let Some(header) = envelope_header(src, n) else { return };
    let env = unsafe { std::slice::from_raw_parts_mut(src, header) };
    let tail50 = if env[0] == 0xA6 { Some(&env[0x78..0xAA]) } else { None };
    if let Some(key) = server_key(&env[0x24..0x38], &env[0x48..0x58], tail50) {
        env[0x04..0x24].copy_from_slice(&key);
    }
}

struct Armed {
    report: Vec<u8>,
    generation: u64,
    at: Instant,
    key: Option<[u8; 32]>,
}

static ARMED: Mutex<Option<Armed>> = Mutex::new(None);

fn server_key(tail20: &[u8], udid: &[u8], tail50: Option<&[u8]>) -> Option<[u8; 32]> {
    let report: Vec<u8> = [tail20, udid, tail50.unwrap_or(&[])].concat();
    let generation = GENERATION.load(Ordering::Acquire);
    let mut guard = ARMED.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(a) = guard.as_ref() {
        if a.report == report && a.generation == generation && a.at.elapsed() < RECHECK {
            return a.key;
        }
    }
    let arm_host = {
        let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
        if cfg.enabled {
            Some(cfg.host.clone())
        } else if dns_redirected() {
            Some("127.0.0.1".to_string())
        } else {
            None
        }
    };
    let key = arm_host.and_then(|h| fetch_key(&h, tail20, udid, tail50));
    let was = guard.as_ref().map(|a| a.key.is_some());
    if was != Some(key.is_some()) {
        if key.is_some() {
            log(3, "ARMED: sealing to the custom server");
        } else {
            log(3, "not armed: requests sealed for Cygames untouched (redirect off or the server's arming channel is down)");
        }
    }
    *guard = Some(Armed { report, generation, at: Instant::now(), key });
    key
}

fn dns_redirected() -> bool {
    match (JP_HOST, 443).to_socket_addrs() {
        Ok(addrs) => {
            let addrs: Vec<SocketAddr> = addrs.collect();
            !addrs.is_empty() && addrs.iter().all(|a| a.ip().is_loopback())
        }
        Err(_) => false,
    }
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn fetch_key(host: &str, tail20: &[u8], udid: &[u8], tail50: Option<&[u8]>) -> Option<[u8; 32]> {
    let port = std::env::var("UMA_JP_ARM_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(DEFAULT_ARM_PORT);
    let loopback = host == "localhost" || host.parse::<std::net::IpAddr>().map(|ip| ip.is_loopback()).unwrap_or(false);
    let addr = if loopback {
        SocketAddr::from(([127, 0, 0, 1], port))
    } else {
        (host, port).to_socket_addrs().ok()?.next()?
    };
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_millis(300)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(1))).ok()?;
    let req = format!(
        "GET /arm?tail20={}&udid={}&tail50={} HTTP/1.0\r\nHost: {host}\r\n\r\n",
        hex(tail20),
        hex(udid),
        hex(tail50.unwrap_or(&[]))
    );
    s.write_all(req.as_bytes()).ok()?;
    let mut resp = Vec::new();
    s.read_to_end(&mut resp).ok()?;
    let text = String::from_utf8(resp).ok()?;
    let (head, body) = text.split_once("\r\n\r\n")?;
    if !head.starts_with("HTTP/1.0 200") && !head.starts_with("HTTP/1.1 200") {
        return None;
    }
    let body = body.trim();
    if body.len() != 64 {
        return None;
    }
    let mut key = [0u8; 32];
    for (i, k) in key.iter_mut().enumerate() {
        *k = u8::from_str_radix(&body[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(key)
}

fn already_detoured(addr: usize) -> bool {
    let p = unsafe { std::slice::from_raw_parts(addr as *const u8, 2) };
    p[0] == 0xE9 || p[0] == 0xEB || (p[0] == 0xFF && p[1] == 0x25)
}

fn set_seal_state(s: &'static str) {
    *SEAL_STATE.lock().unwrap_or_else(|e| e.into_inner()) = s;
}

fn try_install_seal() -> bool {
    let addr = export("libnative.dll", "LZ4_compress_default_ext", false);
    if addr == 0 {
        return false;
    }
    if already_detoured(addr) {
        log(1, "LZ4_compress_default_ext is already hooked (hachimi_jp_localserver or umacap loaded too?); NOT patching");
        set_seal_state("blocked: another plugin hooks LZ4 (remove hachimi_jp_localserver / umacap)");
        return true;
    }
    let tramp = interceptor_hook(addr, hk_compress as *mut c_void);
    if tramp == 0 {
        log(1, "failed to hook LZ4_compress_default_ext; NOT patching");
        set_seal_state("failed to hook LZ4_compress_default_ext");
        return true;
    }
    LZ4_TRAMPOLINE.store(tramp, Ordering::Release);
    set_seal_state("ready");
    log(3, "hooked libnative.dll LZ4_compress_default_ext");

    let dec = export("libnative.dll", "LZ4_decompress_safe_ext", false);
    if dec == 0 || already_detoured(dec) {
        log(2, "LZ4_decompress_safe_ext missing or already hooked; replies will not be captured");
    } else {
        let t = interceptor_hook(dec, hk_decompress as *mut c_void);
        if t == 0 {
            log(2, "failed to hook LZ4_decompress_safe_ext; replies will not be captured");
        } else {
            LZ4_DEC_TRAMPOLINE.store(t, Ordering::Release);
            REPLY_HOOKED.store(true, Ordering::Release);
            log(3, "hooked libnative.dll LZ4_decompress_safe_ext");
        }
    }
    true
}

static REPLY_HOOKED: AtomicBool = AtomicBool::new(false);
static CAPTURED: AtomicU64 = AtomicU64::new(0);

struct CaptureFile {
    file: Option<std::fs::File>,
    path: String,
    seq: u64,
}

static CAPTURE_FILE: Mutex<CaptureFile> = Mutex::new(CaptureFile { file: None, path: String::new(), seq: 0 });

fn capturing() -> bool {
    region() == Region::Jp && CAPTURE.load(Ordering::Acquire) && !CONFIG.read().unwrap_or_else(|e| e.into_inner()).enabled
}

fn capture_request(src: *const u8, n: i32) {
    if !capturing() {
        return;
    }
    let Some(header) = envelope_header(src, n) else { return };
    let body = unsafe { std::slice::from_raw_parts(src.add(header), n as usize - header) };
    let Some(json) = msgpack_json(body) else { return };
    let env = format!("0x{:02X}", unsafe { *src });
    capture_write("api_request", &format!(",\"env_type\":\"{env}\",\"len\":{n},\"body_off\":{header},\"json\":{json}"));
}

fn capture_reply(dst: *const u8, n: i32) {
    if dst.is_null() || !capturing() {
        return;
    }
    let buf = unsafe { std::slice::from_raw_parts(dst, n as usize) };
    if !matches!(buf[0], 0x80..=0x8f | 0xde | 0xdf) {
        return;
    }
    let Some(json) = msgpack_json(buf) else { return };
    capture_write("api_reply", &format!(",\"len\":{n},\"json\":{json}"));
}

fn capture_write(kind: &str, fields: &str) {
    let mut c = CAPTURE_FILE.lock().unwrap_or_else(|e| e.into_inner());
    if c.file.is_none() {
        let Some(dir) = config_path().and_then(|p| p.parent().map(|d| d.join("umacap"))) else { return };
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join(format!("api-{}-{}.jsonl", utc_stamp(), std::process::id()));
        match std::fs::OpenOptions::new().create(true).append(true).open(&path) {
            Ok(f) => {
                log(3, &format!("capturing API traffic to {}", path.display()));
                c.path = path.display().to_string();
                c.file = Some(f);
            }
            Err(e) => {
                log(1, &format!("could not open capture file {}: {e}; capture off", path.display()));
                CAPTURE.store(false, Ordering::Release);
                return;
            }
        }
    }
    let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    let line = format!("{{\"client\":\"jp\",\"kind\":\"{kind}\",\"seq\":{},\"t\":{t:.6}{fields}}}\n", c.seq);
    c.seq += 1;
    if let Some(f) = c.file.as_mut() {
        if f.write_all(line.as_bytes()).is_ok() {
            CAPTURED.fetch_add(1, Ordering::Relaxed);
        }
    }
}

fn utc_stamp() -> String {
    let s = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) as i64;
    let (days, rem) = (s.div_euclid(86_400), s.rem_euclid(86_400));
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}{m:02}{d:02}-{:02}{:02}{:02}", rem / 3600, rem % 3600 / 60, rem % 60)
}

fn json_str(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for ch in s.chars() {
        match ch {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn base64(b: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut o = String::with_capacity(b.len().div_ceil(3) * 4);
    for c in b.chunks(3) {
        let n = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= c.len() {
                o.push(T[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                o.push('=');
            }
        }
    }
    o
}

fn msgpack_json(b: &[u8]) -> Option<String> {
    let mut p = Mp { b, i: 0 };
    let mut out = String::with_capacity(b.len() * 2);
    p.value(&mut out, 0)?;
    (p.i == b.len()).then_some(out)
}

struct Mp<'a> {
    b: &'a [u8],
    i: usize,
}

impl Mp<'_> {
    fn take(&mut self, n: usize) -> Option<&[u8]> {
        let s = self.b.get(self.i..self.i.checked_add(n)?)?;
        self.i += n;
        Some(s)
    }
    fn uint(&mut self, n: usize) -> Option<u64> {
        Some(self.take(n)?.iter().fold(0u64, |a, &x| a << 8 | x as u64))
    }
    fn value(&mut self, o: &mut String, depth: u32) -> Option<()> {
        if depth > 100 {
            return None;
        }
        let t = *self.take(1)?.first()?;
        match t {
            0x00..=0x7f => o.push_str(&t.to_string()),
            0xe0..=0xff => o.push_str(&(t as i8).to_string()),
            0xc0 => o.push_str("null"),
            0xc2 => o.push_str("false"),
            0xc3 => o.push_str("true"),
            0xcc..=0xcf => o.push_str(&self.uint(1 << (t - 0xcc))?.to_string()),
            0xd0..=0xd3 => {
                let n = 1usize << (t - 0xd0);
                let v = self.uint(n)?;
                let shift = 64 - 8 * n as u32;
                o.push_str(&(((v << shift) as i64) >> shift).to_string());
            }
            0xca | 0xcb => {
                let f = if t == 0xca { f32::from_bits(self.uint(4)? as u32) as f64 } else { f64::from_bits(self.uint(8)?) };
                o.push_str(&if f.is_finite() { format!("{f:?}") } else { "null".into() });
            }
            0xa0..=0xbf | 0xd9..=0xdb => {
                let n = if t <= 0xbf { (t & 0x1f) as usize } else { self.uint(1 << (t - 0xd9))? as usize };
                o.push_str(&json_str(&String::from_utf8_lossy(self.take(n)?)));
            }
            0xc4..=0xc6 => {
                let n = self.uint(1 << (t - 0xc4))? as usize;
                o.push_str(&json_str(&base64(self.take(n)?)));
            }
            0x90..=0x9f | 0xdc | 0xdd => {
                let n = if t <= 0x9f { (t & 0x0f) as usize } else { self.uint(if t == 0xdc { 2 } else { 4 })? as usize };
                o.push('[');
                for k in 0..n {
                    if k > 0 {
                        o.push(',');
                    }
                    self.value(o, depth + 1)?;
                }
                o.push(']');
            }
            0x80..=0x8f | 0xde | 0xdf => {
                let n = if t <= 0x8f { (t & 0x0f) as usize } else { self.uint(if t == 0xde { 2 } else { 4 })? as usize };
                o.push('{');
                for k in 0..n {
                    if k > 0 {
                        o.push(',');
                    }
                    let mut key = String::new();
                    self.value(&mut key, depth + 1)?;
                    if key.starts_with('"') {
                        o.push_str(&key);
                    } else {
                        o.push_str(&json_str(&key));
                    }
                    o.push(':');
                    self.value(o, depth + 1)?;
                }
                o.push('}');
            }
            0xd4..=0xd8 | 0xc7..=0xc9 => {
                let n = match t {
                    0xd4..=0xd8 => 1usize << (t - 0xd4),
                    _ => self.uint(1 << (t - 0xc7))? as usize,
                };
                self.take(n + 1)?;
                o.push_str("null");
            }
            _ => return None,
        }
        Some(())
    }
}

fn spawn_seal_installer() {
    let _ = std::thread::Builder::new().name("redirect-seal-install".into()).spawn(|| {
        for _ in 0..12_000 {
            if try_install_seal() {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        set_seal_state("libnative.dll never loaded");
        log(2, "libnative.dll never loaded; not patching");
    });
}

static CLIENT_LOG: AtomicBool = AtomicBool::new(false);
static CLIENT_LOG_INSTALLED: OnceLock<bool> = OnceLock::new();
static CLIENT_LOGGED: AtomicU64 = AtomicU64::new(0);
const EXCEPTION_REPEATS: u32 = 3;
const DISTINCT_EXCEPTIONS_MAX: usize = 20_000;

type CtorFn = unsafe extern "C" fn(*mut c_void, *const c_void);
type CtorMsgFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *const c_void);
type SetBoolFn = unsafe extern "C" fn(*mut c_void, bool, *const c_void);
type SetIntFn = unsafe extern "C" fn(*mut c_void, i32, *const c_void);
type LogExceptionFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *const c_void);
type Invoke = unsafe extern "C" fn(*const c_void, *mut c_void, *mut *mut c_void, *mut *mut c_void) -> *mut c_void;

static CTOR_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static CTOR_MSG_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static LOG_ENABLED_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static FILTER_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static LOG_EXCEPTION_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);

struct ClientLogMethods {
    stack_trace: usize,
    to_string: usize,
    unity_logger: usize,
    set_log_enabled: usize,
    set_filter: usize,
}

static CLIENT_LOG_METHODS: OnceLock<ClientLogMethods> = OnceLock::new();
static CLIENT_LOG_FILE: Mutex<Option<std::fs::File>> = Mutex::new(None);
static CLIENT_LOG_PATH: Mutex<String> = Mutex::new(String::new());
static EXCEPTION_COUNTS: Mutex<Option<std::collections::HashMap<String, u32>>> = Mutex::new(None);

thread_local! {
    static RECORDING: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

const LOG_TYPE_ALL: i32 = 3;

unsafe extern "C" fn hk_exception_ctor(this: *mut c_void, mi: *const c_void) {
    std::mem::transmute::<usize, CtorFn>(trampoline(&CTOR_TRAMPOLINE))(this, mi);
    record_exception(this, std::ptr::null_mut(), false);
}

unsafe extern "C" fn hk_exception_ctor_msg(this: *mut c_void, msg: *mut c_void, mi: *const c_void) {
    std::mem::transmute::<usize, CtorMsgFn>(trampoline(&CTOR_MSG_TRAMPOLINE))(this, msg, mi);
    record_exception(this, msg, false);
}

unsafe extern "C" fn hk_set_log_enabled(this: *mut c_void, value: bool, mi: *const c_void) {
    let value = value || CLIENT_LOG.load(Ordering::Acquire);
    std::mem::transmute::<usize, SetBoolFn>(trampoline(&LOG_ENABLED_TRAMPOLINE))(this, value, mi);
}

unsafe extern "C" fn hk_set_filter_log_type(this: *mut c_void, value: i32, mi: *const c_void) {
    let value = if CLIENT_LOG.load(Ordering::Acquire) { LOG_TYPE_ALL } else { value };
    std::mem::transmute::<usize, SetIntFn>(trampoline(&FILTER_TRAMPOLINE))(this, value, mi);
}

unsafe extern "C" fn hk_log_exception(this: *mut c_void, ex: *mut c_void, ctx: *mut c_void, mi: *const c_void) {
    record_exception(ex, std::ptr::null_mut(), true);
    std::mem::transmute::<usize, LogExceptionFn>(trampoline(&LOG_EXCEPTION_TRAMPOLINE))(this, ex, ctx, mi);
}

fn record_exception(ex: *mut c_void, msg: *mut c_void, logged: bool) {
    if ex.is_null() || !CLIENT_LOG.load(Ordering::Acquire) || RECORDING.with(|r| r.replace(true)) {
        return;
    }
    let _ = std::panic::catch_unwind(|| record_exception_inner(ex, msg, logged));
    RECORDING.with(|r| r.set(false));
}

fn record_exception_inner(ex: *mut c_void, msg: *mut c_void, logged: bool) {
    let name = class_name(ex).unwrap_or_else(|| "?".into());
    let message = il2cpp_text(msg).unwrap_or_default();
    let key = format!("{}|{name}|{message}", if logged { "L" } else { "C" });
    let n = {
        let mut counts = EXCEPTION_COUNTS.lock().unwrap_or_else(|e| e.into_inner());
        let counts = counts.get_or_insert_with(Default::default);
        if counts.len() >= DISTINCT_EXCEPTIONS_MAX && !counts.contains_key(&key) {
            return;
        }
        let n = counts.entry(key).or_insert(0);
        *n += 1;
        *n
    };
    if n > EXCEPTION_REPEATS {
        return;
    }
    let methods = CLIENT_LOG_METHODS.get();
    let entry = if logged {
        let text = methods.and_then(|m| invoke_text(m.to_string, ex)).unwrap_or_else(|| name.clone());
        format!("LOGGED {text}")
    } else {
        let stack = methods.and_then(|m| invoke_text(m.stack_trace, std::ptr::null_mut())).unwrap_or_default();
        let repeat = if n == EXCEPTION_REPEATS { " (further repeats not shown)" } else { "" };
        format!("CREATED {name}: {message}{repeat}\n{stack}")
    };
    client_log_write(&entry);
}

fn class_name(obj: *mut c_void) -> Option<String> {
    let get_class: unsafe extern "C" fn(*mut c_void) -> *mut c_void = il2cpp("il2cpp_object_get_class")?;
    let name: unsafe extern "C" fn(*mut c_void) -> *const c_char = il2cpp("il2cpp_class_get_name")?;
    let ns: unsafe extern "C" fn(*mut c_void) -> *const c_char = il2cpp("il2cpp_class_get_namespace")?;
    unsafe {
        let k = get_class(obj);
        if k.is_null() {
            return None;
        }
        let text = |p: *const c_char| if p.is_null() { String::new() } else { CStr::from_ptr(p).to_string_lossy().into_owned() };
        let (n, s) = (text(name(k)), text(ns(k)));
        Some(if s.is_empty() { n } else { format!("{s}.{n}") })
    }
}

fn invoke_text(method: usize, this: *mut c_void) -> Option<String> {
    let invoke: Invoke = il2cpp("il2cpp_runtime_invoke")?;
    let mut exc: *mut c_void = std::ptr::null_mut();
    let s = unsafe { invoke(method as *const c_void, this, std::ptr::null_mut(), &mut exc) };
    if exc.is_null() {
        il2cpp_text(s)
    } else {
        None
    }
}

fn client_log_write(entry: &str) {
    let mut file = CLIENT_LOG_FILE.lock().unwrap_or_else(|e| e.into_inner());
    if file.is_none() {
        let Some(dir) = config_path().and_then(|p| p.parent().map(|d| d.join("clientlog"))) else { return };
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join(format!("client-{}-{}.log", utc_stamp(), std::process::id()));
        match std::fs::OpenOptions::new().create(true).append(true).open(&path) {
            Ok(f) => {
                log(3, &format!("client log -> {}", path.display()));
                *CLIENT_LOG_PATH.lock().unwrap_or_else(|e| e.into_inner()) = path.display().to_string();
                *file = Some(f);
            }
            Err(e) => {
                log(1, &format!("could not open client log {}: {e}; client log off", path.display()));
                CLIENT_LOG.store(false, Ordering::Release);
                return;
            }
        }
    }
    let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0);
    if let Some(f) = file.as_mut() {
        if f.write_all(format!("[{t:.3}] {entry}\n").as_bytes()).is_ok() {
            let _ = f.flush();
            CLIENT_LOGGED.fetch_add(1, Ordering::Relaxed);
        }
    }
}

fn set_client_log(on: bool) {
    CLIENT_LOG.store(on, Ordering::Release);
    if on {
        try_install_client_log();
        reopen_unity_logger();
    }
}

fn try_install_client_log() {
    if CLIENT_LOG_INSTALLED.get().is_some() {
        return;
    }
    let ok = unsafe { install_client_log() };
    if let Err(e) = &ok {
        log(1, &format!("client log not installed ({e})"));
    }
    let _ = CLIENT_LOG_INSTALLED.set(ok.is_ok());
}

unsafe fn install_client_log() -> Result<(), String> {
    type DomainGet = unsafe extern "C" fn() -> *mut c_void;
    type ThreadAttach = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type MethodFromName = unsafe extern "C" fn(*mut c_void, *const c_char, i32) -> *const c_void;

    let api = API.get().ok_or("no API")?;
    let (get_image, get_class, get_addr) = (
        api.get_image.ok_or("no il2cpp API")?,
        api.get_class.ok_or("no il2cpp API")?,
        api.get_method_addr.ok_or("no il2cpp API")?,
    );
    let missing = |n: &str| format!("il2cpp export {n} unavailable");
    let domain_get: DomainGet = il2cpp("il2cpp_domain_get").ok_or_else(|| missing("domain_get"))?;
    let attach: ThreadAttach = il2cpp("il2cpp_thread_attach").ok_or_else(|| missing("thread_attach"))?;
    let from_name: MethodFromName =
        il2cpp("il2cpp_class_get_method_from_name").ok_or_else(|| missing("class_get_method_from_name"))?;
    il2cpp::<Invoke>("il2cpp_runtime_invoke").ok_or_else(|| missing("runtime_invoke"))?;
    attach(domain_get());

    let corlib = get_image(c"mscorlib.dll".as_ptr());
    let core = get_image(c"UnityEngine.CoreModule.dll".as_ptr());
    if corlib.is_null() || core.is_null() {
        return Err("mscorlib.dll / UnityEngine.CoreModule.dll image not found".into());
    }
    let exception = get_class(corlib, c"System".as_ptr(), c"Exception".as_ptr());
    let environment = get_class(corlib, c"System".as_ptr(), c"Environment".as_ptr());
    let logger = get_class(core, c"UnityEngine".as_ptr(), c"Logger".as_ptr());
    let debug = get_class(core, c"UnityEngine".as_ptr(), c"Debug".as_ptr());
    if exception.is_null() || environment.is_null() {
        return Err("System.Exception / System.Environment not found".into());
    }
    let mi = |k: *mut c_void, n: &CStr, argc: i32| if k.is_null() { 0 } else { from_name(k, n.as_ptr(), argc) as usize };
    let _ = CLIENT_LOG_METHODS.set(ClientLogMethods {
        stack_trace: mi(environment, c"get_StackTrace", 0),
        to_string: mi(exception, c"ToString", 0),
        unity_logger: mi(debug, c"get_unityLogger", 0),
        set_log_enabled: mi(logger, c"set_logEnabled", 1),
        set_filter: mi(logger, c"set_filterLogType", 1),
    });

    let mut hooked = Vec::new();
    let mut failed = Vec::new();
    let hooks: [(*mut c_void, &CStr, i32, *mut c_void, &AtomicUsize, &str); 5] = [
        (exception, c".ctor", 0, hk_exception_ctor as *mut c_void, &CTOR_TRAMPOLINE, "Exception()"),
        (exception, c".ctor", 1, hk_exception_ctor_msg as *mut c_void, &CTOR_MSG_TRAMPOLINE, "Exception(string)"),
        (logger, c"set_logEnabled", 1, hk_set_log_enabled as *mut c_void, &LOG_ENABLED_TRAMPOLINE, "Logger.set_logEnabled"),
        (logger, c"set_filterLogType", 1, hk_set_filter_log_type as *mut c_void, &FILTER_TRAMPOLINE, "Logger.set_filterLogType"),
        (logger, c"LogException", 2, hk_log_exception as *mut c_void, &LOG_EXCEPTION_TRAMPOLINE, "Logger.LogException"),
    ];
    for (class, name, argc, detour, slot, label) in hooks {
        let addr = if class.is_null() { 0 } else { get_addr(class, name.as_ptr(), argc) as usize };
        let tramp = if addr == 0 { 0 } else { interceptor_hook(addr, detour) };
        if tramp == 0 {
            failed.push(label);
        } else {
            slot.store(tramp, Ordering::Release);
            hooked.push(label);
        }
    }
    if hooked.is_empty() {
        return Err(format!("nothing could be hooked ({})", failed.join(", ")));
    }
    log(3, &format!("client log hooks: {}", hooked.join(", ")));
    if !failed.is_empty() {
        log(2, &format!("client log hooks missing: {}", failed.join(", ")));
    }
    Ok(())
}

fn reopen_unity_logger() {
    let Some(m) = CLIENT_LOG_METHODS.get() else { return };
    if m.unity_logger == 0 {
        return;
    }
    let Some(invoke) = il2cpp::<Invoke>("il2cpp_runtime_invoke") else { return };
    let _ = std::panic::catch_unwind(|| unsafe {
        if let (Some(domain_get), Some(attach)) = (
            il2cpp::<unsafe extern "C" fn() -> *mut c_void>("il2cpp_domain_get"),
            il2cpp::<unsafe extern "C" fn(*mut c_void) -> *mut c_void>("il2cpp_thread_attach"),
        ) {
            attach(domain_get());
        }
        let mut exc: *mut c_void = std::ptr::null_mut();
        let logger = invoke(m.unity_logger as *const c_void, std::ptr::null_mut(), std::ptr::null_mut(), &mut exc);
        if logger.is_null() || !exc.is_null() {
            return;
        }
        let mut on = true;
        let mut all = LOG_TYPE_ALL;
        if m.set_log_enabled != 0 {
            let mut args = [&mut on as *mut bool as *mut c_void];
            invoke(m.set_log_enabled as *const c_void, logger, args.as_mut_ptr(), &mut exc);
        }
        if m.set_filter != 0 {
            let mut args = [&mut all as *mut i32 as *mut c_void];
            invoke(m.set_filter as *const c_void, logger, args.as_mut_ptr(), &mut exc);
        }
        log(3, "Debug.unityLogger reopened (logEnabled, all log types)");
    });
}

const RACE_HORSE_MAX: i32 = 18;
static BIG_FIELDS: AtomicBool = AtomicBool::new(true);
static BIG_FIELDS_INSTALLED: OnceLock<bool> = OnceLock::new();
static BIG_FIELDS_WIDENED: AtomicU64 = AtomicU64::new(0);
static TAG_HORSE_NUM_FIELD: AtomicUsize = AtomicUsize::new(0);
static TAG_GLOBAL_INDEX_FIELD: AtomicUsize = AtomicUsize::new(0);
static TAG_HORSE_KEYS_FIELD: AtomicUsize = AtomicUsize::new(0);
static TAG_CTOR_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static TAG_UPDATE_RACE_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);

type TagCtorFn = unsafe extern "C" fn(*mut c_void, *mut c_void, i32, *mut c_void, *mut c_void, *mut c_void, *mut c_void, *mut c_void, *const c_void);
type TagUpdateRaceFn = unsafe extern "C" fn(*mut c_void, usize, usize, *const c_void);

unsafe extern "C" fn hk_jikkyo_tag_ctor(
    this: *mut c_void,
    course_set: *mut c_void,
    race_id: i32,
    race_name: *mut c_void,
    horse_array: *mut c_void,
    by_popularity: *mut c_void,
    by_order: *mut c_void,
    player_index: *mut c_void,
    mi: *const c_void,
) {
    std::mem::transmute::<usize, TagCtorFn>(trampoline(&TAG_CTOR_TRAMPOLINE))(
        this, course_set, race_id, race_name, horse_array, by_popularity, by_order, player_index, mi,
    );
    let _ = std::panic::catch_unwind(|| {
        if let Some(n) = big_field_size(this) {
            let global = object_field(this, &TAG_GLOBAL_INDEX_FIELD);
            let widened = widen_array_fields(global, n);
            note_widened("GlobalTagIndex", widened, n);
        }
    });
}

unsafe extern "C" fn hk_jikkyo_update_tag_race(this: *mut c_void, scene: usize, param: usize, mi: *const c_void) {
    std::mem::transmute::<usize, TagUpdateRaceFn>(trampoline(&TAG_UPDATE_RACE_TRAMPOLINE))(this, scene, param, mi);
    let _ = std::panic::catch_unwind(|| {
        if let Some(n) = big_field_size(this) {
            let field = TAG_HORSE_KEYS_FIELD.load(Ordering::Acquire) as *mut c_void;
            if widen_array(this, field, n) {
                note_widened("_horseTagKeys", 1, n);
            }
        }
    });
}

fn note_widened(what: &str, count: usize, n: i32) {
    if count > 0 && BIG_FIELDS_WIDENED.fetch_add(1, Ordering::Relaxed) < 8 {
        let line = format!("big field: widened {count} {what} table(s) to {n} runners");
        log(3, &line);
        if CLIENT_LOG.load(Ordering::Acquire) {
            client_log_write(&line);
        }
    }
}

type FieldGetValue = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void);

fn big_field_size(obj: *mut c_void) -> Option<i32> {
    if obj.is_null() || !BIG_FIELDS.load(Ordering::Acquire) {
        return None;
    }
    let get: FieldGetValue = il2cpp("il2cpp_field_get_value")?;
    let field = TAG_HORSE_NUM_FIELD.load(Ordering::Acquire) as *mut c_void;
    if field.is_null() {
        return None;
    }
    let mut n: i32 = 0;
    unsafe { get(obj, field, &mut n as *mut i32 as *mut c_void) };
    (n > RACE_HORSE_MAX).then_some(n)
}

fn object_field(obj: *mut c_void, field: &AtomicUsize) -> *mut c_void {
    let field = field.load(Ordering::Acquire) as *mut c_void;
    let Some(get) = il2cpp::<FieldGetValue>("il2cpp_field_get_value") else { return std::ptr::null_mut() };
    if obj.is_null() || field.is_null() {
        return std::ptr::null_mut();
    }
    let mut v: *mut c_void = std::ptr::null_mut();
    unsafe { get(obj, field, &mut v as *mut *mut c_void as *mut c_void) };
    v
}

fn widen_array_fields(obj: *mut c_void, n: i32) -> usize {
    type GetClass = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type GetFields = unsafe extern "C" fn(*mut c_void, *mut *mut c_void) -> *mut c_void;
    type FieldType = unsafe extern "C" fn(*mut c_void) -> *const c_void;
    type TypeKind = unsafe extern "C" fn(*const c_void) -> i32;
    type FieldFlags = unsafe extern "C" fn(*mut c_void) -> i32;
    const IL2CPP_TYPE_SZARRAY: i32 = 0x1d;
    const FIELD_ATTRIBUTE_STATIC: i32 = 0x10;
    let (Some(get_class), Some(fields), Some(field_type), Some(type_kind), Some(flags)) = (
        il2cpp::<GetClass>("il2cpp_object_get_class"),
        il2cpp::<GetFields>("il2cpp_class_get_fields"),
        il2cpp::<FieldType>("il2cpp_field_get_type"),
        il2cpp::<TypeKind>("il2cpp_type_get_type"),
        il2cpp::<FieldFlags>("il2cpp_field_get_flags"),
    ) else {
        return 0;
    };
    if obj.is_null() {
        return 0;
    }
    let mut count = 0;
    unsafe {
        let klass = get_class(obj);
        let mut iter: *mut c_void = std::ptr::null_mut();
        loop {
            let f = fields(klass, &mut iter);
            if f.is_null() {
                break;
            }
            if flags(f) & FIELD_ATTRIBUTE_STATIC != 0 || type_kind(field_type(f)) != IL2CPP_TYPE_SZARRAY {
                continue;
            }
            if widen_array(obj, f, n) {
                count += 1;
            }
        }
    }
    count
}

fn widen_array(obj: *mut c_void, field: *mut c_void, n: i32) -> bool {
    type GetClass = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type ElementClass = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type ElementSize = unsafe extern "C" fn(*const c_void) -> i32;
    type ArrayNew = unsafe extern "C" fn(*mut c_void, usize) -> *mut c_void;
    let (Some(get), Some(get_class), Some(element_class), Some(element_size), Some(array_new), Some(set)) = (
        il2cpp::<FieldGetValue>("il2cpp_field_get_value"),
        il2cpp::<GetClass>("il2cpp_object_get_class"),
        il2cpp::<ElementClass>("il2cpp_class_get_element_class"),
        il2cpp::<ElementSize>("il2cpp_array_element_size"),
        il2cpp::<ArrayNew>("il2cpp_array_new"),
        API.get().and_then(|a| a.set_field),
    ) else {
        return false;
    };
    if obj.is_null() || field.is_null() || n <= 0 {
        return false;
    }
    unsafe {
        let mut arr: *mut c_void = std::ptr::null_mut();
        get(obj, field, &mut arr as *mut *mut c_void as *mut c_void);
        if arr.is_null() {
            return false;
        }
        let len = *((arr as *const u8).add(0x18) as *const usize);
        if len == 0 || len >= n as usize {
            return false;
        }
        let array_class = get_class(arr);
        let esz = element_size(array_class as *const c_void);
        if esz <= 0 {
            return false;
        }
        let esz = esz as usize;
        let wide = array_new(element_class(array_class), n as usize);
        if wide.is_null() {
            return false;
        }
        let src = (arr as *const u8).add(0x20);
        let dst = (wide as *mut u8).add(0x20);
        type IsValueType = unsafe extern "C" fn(*const c_void) -> bool;
        type Barrier = unsafe extern "C" fn(*mut c_void, *mut *mut c_void, *mut c_void);
        let is_value = il2cpp::<IsValueType>("il2cpp_class_is_valuetype")
            .map(|f| f(element_class(array_class) as *const c_void))
            .unwrap_or(true);
        let barrier = il2cpp::<Barrier>("il2cpp_gc_wbarrier_set_field");
        if !is_value && esz == 8 {
            let src = src as *const *mut c_void;
            let dst = dst as *mut *mut c_void;
            for i in 0..n as usize {
                let v = *src.add(i.min(len - 1));
                match barrier {
                    Some(b) => b(wide, dst.add(i), v),
                    None => *dst.add(i) = v,
                }
            }
        } else {
            std::ptr::copy_nonoverlapping(src, dst, len * esz);
            let last = src.add((len - 1) * esz);
            for i in len..n as usize {
                std::ptr::copy_nonoverlapping(last, dst.add(i * esz), esz);
            }
        }
        set(obj, field, wide as *const c_void);
    }
    true
}

static POOL_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);

struct PoolRefs {
    accessor_horse_number: usize,
    accessor_horse_info: usize,
    create_skill: usize,
    create_skill_array: usize,
    skill_manager_offset: usize,
    skill_array_offset: usize,
}

static POOL_REFS: OnceLock<PoolRefs> = OnceLock::new();

type PoolFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *const c_void);

unsafe extern "C" fn hk_create_pool(this: *mut c_void, accessor: *mut c_void, mi: *const c_void) {
    let _ = std::panic::catch_unwind(|| repair_horse_skills(accessor));
    std::mem::transmute::<usize, PoolFn>(trampoline(&POOL_TRAMPOLINE))(this, accessor, mi);
}

fn invoke_method(method: usize, obj: *mut c_void, args: &mut [*mut c_void]) -> Option<*mut c_void> {
    type Virtual = unsafe extern "C" fn(*mut c_void, *const c_void) -> *const c_void;
    let invoke: Invoke = il2cpp("il2cpp_runtime_invoke")?;
    let resolve: Virtual = il2cpp("il2cpp_object_get_virtual_method")?;
    if method == 0 || obj.is_null() {
        return None;
    }
    unsafe {
        let m = resolve(obj, method as *const c_void);
        let m = if m.is_null() { method as *const c_void } else { m };
        let mut exc: *mut c_void = std::ptr::null_mut();
        let ptr = if args.is_empty() { std::ptr::null_mut() } else { args.as_mut_ptr() };
        let r = invoke(m, obj, ptr, &mut exc);
        exc.is_null().then_some(r)
    }
}

fn repair_horse_skills(accessor: *mut c_void) {
    if accessor.is_null() || !BIG_FIELDS.load(Ordering::Acquire) {
        return;
    }
    let Some(r) = POOL_REFS.get() else { return };
    let Some(boxed) = invoke_method(r.accessor_horse_number, accessor, &mut []) else { return };
    if boxed.is_null() {
        return;
    }
    let n = unsafe { *((boxed as *const u8).add(0x10) as *const i32) };
    CURRENT_FIELD.store(n.max(0) as usize, Ordering::Release);
    let mut repaired = Vec::new();
    for i in 0..n.max(0) {
        let mut idx = i;
        let Some(info) = invoke_method(r.accessor_horse_info, accessor, &mut [&mut idx as *mut i32 as *mut c_void]) else {
            continue;
        };
        if info.is_null() {
            continue;
        }
        let read = |obj: *mut c_void, off: usize| unsafe { *((obj as *const u8).add(off) as *const *mut c_void) };
        let mut manager = read(info, r.skill_manager_offset);
        if manager.is_null() {
            invoke_method(r.create_skill, info, &mut []);
            manager = read(info, r.skill_manager_offset);
            repaired.push(format!("#{i}: CreateSkill -> manager {}", if manager.is_null() { "still null" } else { "ok" }));
        }
        if !manager.is_null() && read(manager, r.skill_array_offset).is_null() {
            invoke_method(r.create_skill_array, manager, &mut []);
            let ok = !read(manager, r.skill_array_offset).is_null();
            repaired.push(format!("#{i}: CreateSkillArray -> {}", if ok { "ok" } else { "still null" }));
        }
    }
    if repaired.is_empty() && n <= RACE_HORSE_MAX {
        return;
    }
    let line = if repaired.is_empty() {
        format!("big field: SkillEffectPool.CreatePool: all {n} runners have skills")
    } else {
        format!("big field: SkillEffectPool.CreatePool ({n} runners) repaired {}", repaired.join(", "))
    };
    log(3, &line);
    if CLIENT_LOG.load(Ordering::Acquire) {
        client_log_write(&line);
    }
}

unsafe fn install_skill_pool_fix(get_image: GetImageFn, get_class: GetClassFn, get_addr: GetMethodAddrFn) -> Result<(), String> {
    type MethodFromName = unsafe extern "C" fn(*mut c_void, *const c_char, i32) -> *const c_void;
    type FieldFromName = unsafe extern "C" fn(*mut c_void, *const c_char) -> *mut c_void;
    type FieldOffset = unsafe extern "C" fn(*mut c_void) -> usize;
    let from_name: MethodFromName = il2cpp("il2cpp_class_get_method_from_name").ok_or("no method_from_name")?;
    let field: FieldFromName = il2cpp("il2cpp_class_get_field_from_name").ok_or("no field_from_name")?;
    let offset: FieldOffset = il2cpp("il2cpp_field_get_offset").ok_or("no field_get_offset")?;
    for e in ["il2cpp_runtime_invoke", "il2cpp_object_get_virtual_method"] {
        il2cpp::<*mut c_void>(e).ok_or_else(|| format!("il2cpp export {e} unavailable"))?;
    }
    let uma = get_image(c"umamusume.dll".as_ptr());
    let class = |name: &CStr| get_class(uma, c"Gallop".as_ptr(), name.as_ptr());
    let (pool, accessor, info, manager) =
        (class(c"SkillEffectPool"), class(c"IRaceHorseAccessor"), class(c"HorseRaceInfo"), class(c"SkillManager"));
    if [pool, accessor, info, manager].iter().any(|c| c.is_null()) {
        return Err("SkillEffectPool / IRaceHorseAccessor / HorseRaceInfo / SkillManager not found".into());
    }
    let m = |k: *mut c_void, n: &CStr, argc: i32| from_name(k, n.as_ptr(), argc) as usize;
    let f = |k: *mut c_void, n: &CStr| {
        let fi = field(k, n.as_ptr());
        if fi.is_null() { 0 } else { offset(fi) }
    };
    let refs = PoolRefs {
        accessor_horse_number: m(accessor, c"GetHorseNumber", 0),
        accessor_horse_info: m(accessor, c"GetHorseInfo", 1),
        create_skill: m(info, c"CreateSkill", 0),
        create_skill_array: m(manager, c"CreateSkillArray", 0),
        skill_manager_offset: f(info, c"_skillManager"),
        skill_array_offset: f(manager, c"_skillArray"),
    };
    if refs.accessor_horse_number == 0
        || refs.accessor_horse_info == 0
        || refs.create_skill == 0
        || refs.create_skill_array == 0
        || refs.skill_manager_offset == 0
        || refs.skill_array_offset == 0
    {
        return Err("a CreatePool repair method or field is missing (client changed?)".into());
    }
    let _ = POOL_REFS.set(refs);
    let addr = get_addr(pool, c"CreatePool".as_ptr(), 1) as usize;
    if addr == 0 {
        return Err("SkillEffectPool.CreatePool not found".into());
    }
    let tramp = interceptor_hook(addr, hk_create_pool as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook SkillEffectPool.CreatePool".into());
    }
    POOL_TRAMPOLINE.store(tramp, Ordering::Release);
    log(3, "hooked SkillEffectPool.CreatePool (big fields)");
    Ok(())
}

static EFFECT_WORK_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static EFFECT_WORK_PREFAB_OFFSET: AtomicUsize = AtomicUsize::new(0);
static DROPPED_EFFECTS: Mutex<Vec<String>> = Mutex::new(Vec::new());

type FindOrAddWorkFn = unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void, *const c_void) -> *mut c_void;

unsafe extern "C" fn hk_find_or_add_effect_work(this: *mut c_void, list: *mut c_void, name: *mut c_void, mi: *const c_void) -> *mut c_void {
    let work = std::mem::transmute::<usize, FindOrAddWorkFn>(trampoline(&EFFECT_WORK_TRAMPOLINE))(this, list, name, mi);
    let _ = std::panic::catch_unwind(|| drop_prefabless_work(list, work, name));
    work
}

fn drop_prefabless_work(list: *mut c_void, work: *mut c_void, name: *mut c_void) {
    let prefab_off = EFFECT_WORK_PREFAB_OFFSET.load(Ordering::Acquire);
    if list.is_null() || work.is_null() || prefab_off == 0 || !BIG_FIELDS.load(Ordering::Acquire) {
        return;
    }
    unsafe {
        if !(*((work as *const u8).add(prefab_off) as *const *mut c_void)).is_null() {
            return;
        }
        let items = *((list as *const u8).add(0x10) as *const *mut c_void);
        let size = (list as *mut u8).add(0x18) as *mut i32;
        if items.is_null() || *size <= 0 {
            return;
        }
        let last = (items as *mut u8).add(0x20 + (*size as usize - 1) * 8) as *mut *mut c_void;
        if *last != work {
            return;
        }
        *last = std::ptr::null_mut();
        *size -= 1;
        *((list as *mut u8).add(0x1C) as *mut i32) += 1;
    }
    let effect = il2cpp_text(name).unwrap_or_else(|| "?".into());
    let mut dropped = DROPPED_EFFECTS.lock().unwrap_or_else(|e| e.into_inner());
    if !dropped.contains(&effect) {
        let line = format!("big field: skill effect \"{effect}\" was never loaded; not pooled");
        log(2, &line);
        if CLIENT_LOG.load(Ordering::Acquire) {
            client_log_write(&line);
        }
        dropped.push(effect);
    }
}

unsafe fn install_effect_work_fix(get_image: GetImageFn, get_class: GetClassFn, get_addr: GetMethodAddrFn) -> Result<(), String> {
    type FieldFromName = unsafe extern "C" fn(*mut c_void, *const c_char) -> *mut c_void;
    type FieldOffset = unsafe extern "C" fn(*mut c_void) -> usize;
    let field: FieldFromName = il2cpp("il2cpp_class_get_field_from_name").ok_or("no field_from_name")?;
    let offset: FieldOffset = il2cpp("il2cpp_field_get_offset").ok_or("no field_get_offset")?;
    let uma = get_image(c"umamusume.dll".as_ptr());
    let pool = get_class(uma, c"Gallop".as_ptr(), c"SkillEffectPool".as_ptr());
    if pool.is_null() {
        return Err("SkillEffectPool not found".into());
    }
    let work = get_class(uma, c"Gallop".as_ptr(), c"SkillEffectPool/EffectCacheMakeWork".as_ptr());
    let prefab = if work.is_null() { std::ptr::null_mut() } else { field(work, c"EffectPrefab".as_ptr()) };
    EFFECT_WORK_PREFAB_OFFSET.store(if prefab.is_null() { 0x20 } else { offset(prefab) }, Ordering::Release);
    let addr = get_addr(pool, c"FindOrAddEffectCacheMakeWork".as_ptr(), 2) as usize;
    if addr == 0 {
        return Err("SkillEffectPool.FindOrAddEffectCacheMakeWork not found".into());
    }
    let tramp = interceptor_hook(addr, hk_find_or_add_effect_work as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook FindOrAddEffectCacheMakeWork".into());
    }
    EFFECT_WORK_TRAMPOLINE.store(tramp, Ordering::Release);
    log(3, "hooked SkillEffectPool.FindOrAddEffectCacheMakeWork (big fields)");
    Ok(())
}

const RANK_SPRITES_MAX: usize = 20;
static UI_RANK_SETUP_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static UI_RANK_BIG_FIELD: AtomicUsize = AtomicUsize::new(0);
static UI_RANK_SMALL_FIELD: AtomicUsize = AtomicUsize::new(0);
static UI_RANK_INIT_SPRITES_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static UI_RANK_LAST_N: AtomicUsize = AtomicUsize::new(0);

type UiRankInitSpritesFn = unsafe extern "C" fn(*mut c_void, *const c_void);

unsafe extern "C" fn hk_ui_rank_init_sprites(this: *mut c_void, mi: *const c_void) {
    std::mem::transmute::<usize, UiRankInitSpritesFn>(trampoline(&UI_RANK_INIT_SPRITES_TRAMPOLINE))(this, mi);
    let n = UI_RANK_LAST_N.load(Ordering::Acquire) as i32;
    if BIG_FIELDS.load(Ordering::Acquire) && n > RANK_SPRITES_MAX as i32 {
        let _ = std::panic::catch_unwind(|| widen_rank_sprites(this, n));
    }
}

fn widen_rank_sprites(this: *mut c_void, n: i32) {
    let mut widened = 0;
    for f in [&UI_RANK_BIG_FIELD, &UI_RANK_SMALL_FIELD] {
        if widen_array(this, f.load(Ordering::Acquire) as *mut c_void, n) {
            widened += 1;
        }
    }
    note_widened("RaceUIRank sprite", widened, n);
}

type UiRankSetupFn = unsafe extern "C" fn(
    *mut c_void, usize, usize, usize, usize, usize, usize, usize, usize, usize, *const c_void,
);

unsafe extern "C" fn hk_ui_rank_setup(
    this: *mut c_void,
    index: usize,
    index_max: usize,
    targets: usize,
    distance_target: usize,
    horse_num: usize,
    show_distance: usize,
    hide_distance: usize,
    race_type: usize,
    prefab: usize,
    mi: *const c_void,
) {
    let n = horse_num as u32 as i32;
    let big = BIG_FIELDS.load(Ordering::Acquire) && n > RANK_SPRITES_MAX as i32;
    let passed = if big { RANK_SPRITES_MAX } else { horse_num };
    std::mem::transmute::<usize, UiRankSetupFn>(trampoline(&UI_RANK_SETUP_TRAMPOLINE))(
        this, index, index_max, targets, distance_target, passed, show_distance, hide_distance, race_type, prefab, mi,
    );
    if big {
        UI_RANK_LAST_N.store(n as usize, Ordering::Release);
        let _ = std::panic::catch_unwind(|| widen_rank_sprites(this, n));
    }
}

unsafe fn install_ui_rank_fix(get_image: GetImageFn, get_class: GetClassFn, get_addr: GetMethodAddrFn) -> Result<(), String> {
    let get_field = API.get().and_then(|a| a.get_field).ok_or("no field API")?;
    let uma = get_image(c"umamusume.dll".as_ptr());
    let class = get_class(uma, c"Gallop".as_ptr(), c"RaceUIRank".as_ptr());
    if class.is_null() {
        return Err("Gallop.RaceUIRank not found".into());
    }
    for (name, slot) in [(c"_rankingNumBigSprites", &UI_RANK_BIG_FIELD), (c"_rankingNumSmallSprites", &UI_RANK_SMALL_FIELD)] {
        let f = get_field(class, name.as_ptr()) as usize;
        if f == 0 {
            return Err(format!("RaceUIRank.{} not found", name.to_string_lossy()));
        }
        slot.store(f, Ordering::Release);
    }
    let addr = get_addr(class, c"Setup".as_ptr(), 9) as usize;
    if addr == 0 {
        return Err("RaceUIRank.Setup not found".into());
    }
    let tramp = interceptor_hook(addr, hk_ui_rank_setup as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook RaceUIRank.Setup".into());
    }
    UI_RANK_SETUP_TRAMPOLINE.store(tramp, Ordering::Release);
    let init = get_addr(class, c"InitSprites".as_ptr(), 0) as usize;
    let tramp = if init == 0 { 0 } else { interceptor_hook(init, hk_ui_rank_init_sprites as *mut c_void) };
    if tramp == 0 {
        log(2, "big field: RaceUIRank.InitSprites not hooked; a later sprite reset could overflow at place 21+");
    } else {
        UI_RANK_INIT_SPRITES_TRAMPOLINE.store(tramp, Ordering::Release);
    }
    log(3, "hooked RaceUIRank.Setup / InitSprites (big fields)");
    Ok(())
}

static MINIMAP_SETUP_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);
static MINIMAP_HORSE_NUM_FIELD: AtomicUsize = AtomicUsize::new(0);
static MINIMAP_ICONS_FIELD: AtomicUsize = AtomicUsize::new(0);

type MiniMapSetupFn = unsafe extern "C" fn(*mut c_void, f32, f32, f32, *const c_void);

unsafe extern "C" fn hk_minimap_setup(this: *mut c_void, show: f32, show_time: f32, hide_time: f32, mi: *const c_void) {
    std::mem::transmute::<usize, MiniMapSetupFn>(trampoline(&MINIMAP_SETUP_TRAMPOLINE))(this, show, show_time, hide_time, mi);
    let _ = std::panic::catch_unwind(|| {
        if !BIG_FIELDS.load(Ordering::Acquire) {
            return;
        }
        let Some(get) = il2cpp::<FieldGetValue>("il2cpp_field_get_value") else { return };
        let num = MINIMAP_HORSE_NUM_FIELD.load(Ordering::Acquire) as *mut c_void;
        let mut n: i32 = 0;
        get(this, num, &mut n as *mut i32 as *mut c_void);
        if n > RACE_HORSE_MAX && widen_array(this, MINIMAP_ICONS_FIELD.load(Ordering::Acquire) as *mut c_void, n) {
            note_widened("RaceUIMiniMap icon", 1, n);
        }
    });
}

unsafe fn install_minimap_fix(get_image: GetImageFn, get_class: GetClassFn, get_addr: GetMethodAddrFn) -> Result<(), String> {
    let get_field = API.get().and_then(|a| a.get_field).ok_or("no field API")?;
    let uma = get_image(c"umamusume.dll".as_ptr());
    let class = get_class(uma, c"Gallop".as_ptr(), c"RaceUIMiniMap".as_ptr());
    if class.is_null() {
        return Err("Gallop.RaceUIMiniMap not found".into());
    }
    for (name, slot) in [(c"_horseNum", &MINIMAP_HORSE_NUM_FIELD), (c"_horseIconArray", &MINIMAP_ICONS_FIELD)] {
        let f = get_field(class, name.as_ptr()) as usize;
        if f == 0 {
            return Err(format!("RaceUIMiniMap.{} not found", name.to_string_lossy()));
        }
        slot.store(f, Ordering::Release);
    }
    let addr = get_addr(class, c"SetupUI".as_ptr(), 3) as usize;
    if addr == 0 {
        return Err("RaceUIMiniMap.SetupUI not found".into());
    }
    let tramp = interceptor_hook(addr, hk_minimap_setup as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook RaceUIMiniMap.SetupUI".into());
    }
    MINIMAP_SETUP_TRAMPOLINE.store(tramp, Ordering::Release);
    log(3, "hooked RaceUIMiniMap.SetupUI (big fields)");
    Ok(())
}

static CURRENT_FIELD: AtomicUsize = AtomicUsize::new(0);
static MAX_NUM_TRAMPOLINE: AtomicUsize = AtomicUsize::new(0);

type MaxNumFn = unsafe extern "C" fn(i32, *const c_void) -> i32;

unsafe extern "C" fn hk_race_horse_max_num(race_type: i32, mi: *const c_void) -> i32 {
    let max = std::mem::transmute::<usize, MaxNumFn>(trampoline(&MAX_NUM_TRAMPOLINE))(race_type, mi);
    let field = CURRENT_FIELD.load(Ordering::Acquire) as i32;
    if BIG_FIELDS.load(Ordering::Acquire) && field > max {
        field
    } else {
        max
    }
}

unsafe fn install_camera_fix(get_image: GetImageFn, get_class: GetClassFn, get_addr: GetMethodAddrFn) -> Result<(), String> {
    let uma = get_image(c"umamusume.dll".as_ptr());
    let class = get_class(uma, c"Gallop".as_ptr(), c"RaceUtil".as_ptr());
    if class.is_null() {
        return Err("Gallop.RaceUtil not found".into());
    }
    let addr = get_addr(class, c"GetRaceHorseMaxNum".as_ptr(), 1) as usize;
    if addr == 0 {
        return Err("RaceUtil.GetRaceHorseMaxNum not found".into());
    }
    let tramp = interceptor_hook(addr, hk_race_horse_max_num as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook RaceUtil.GetRaceHorseMaxNum".into());
    }
    MAX_NUM_TRAMPOLINE.store(tramp, Ordering::Release);
    log(3, "hooked RaceUtil.GetRaceHorseMaxNum (big fields: camera targets)");
    Ok(())
}

fn try_install_big_fields() {
    if BIG_FIELDS_INSTALLED.get().is_some() {
        return;
    }
    let ok = unsafe { install_big_fields() };
    if let Err(e) = &ok {
        log(1, &format!("big-field fixes not installed ({e}); races over 18 runners will hang"));
    }
    let _ = BIG_FIELDS_INSTALLED.set(ok.is_ok());
}

unsafe fn install_big_fields() -> Result<(), String> {
    let api = API.get().ok_or("no API")?;
    let (get_image, get_class, get_addr, get_field) = (
        api.get_image.ok_or("no il2cpp API")?,
        api.get_class.ok_or("no il2cpp API")?,
        api.get_method_addr.ok_or("no il2cpp API")?,
        api.get_field.ok_or("no field API")?,
    );
    api.set_field.ok_or("no field API")?;
    for export in [
        "il2cpp_field_get_value",
        "il2cpp_object_get_class",
        "il2cpp_class_get_fields",
        "il2cpp_field_get_type",
        "il2cpp_type_get_type",
        "il2cpp_field_get_flags",
        "il2cpp_class_get_element_class",
        "il2cpp_array_element_size",
        "il2cpp_array_new",
    ] {
        il2cpp::<*mut c_void>(export).ok_or_else(|| format!("il2cpp export {export} unavailable"))?;
    }
    let uma = get_image(c"umamusume.dll".as_ptr());
    if uma.is_null() {
        return Err("umamusume.dll image not found".into());
    }
    let tags = get_class(uma, c"Gallop".as_ptr(), c"JikkyoTagProcessor".as_ptr());
    if tags.is_null() {
        return Err("Gallop.JikkyoTagProcessor not found".into());
    }
    for (name, slot) in [
        (c"_horseNum", &TAG_HORSE_NUM_FIELD),
        (c"_globalTagIndex", &TAG_GLOBAL_INDEX_FIELD),
        (c"_horseTagKeys", &TAG_HORSE_KEYS_FIELD),
    ] {
        let f = get_field(tags, name.as_ptr()) as usize;
        if f == 0 {
            return Err(format!("JikkyoTagProcessor.{} not found (client changed?)", name.to_string_lossy()));
        }
        slot.store(f, Ordering::Release);
    }
    let ctor = get_addr(tags, c".ctor".as_ptr(), 7) as usize;
    let update = get_addr(tags, c"UpdateTagRace".as_ptr(), 2) as usize;
    if ctor == 0 || update == 0 {
        return Err("JikkyoTagProcessor..ctor / UpdateTagRace not found (client changed?)".into());
    }
    let tramp = interceptor_hook(ctor, hk_jikkyo_tag_ctor as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook JikkyoTagProcessor..ctor".into());
    }
    TAG_CTOR_TRAMPOLINE.store(tramp, Ordering::Release);
    let tramp = interceptor_hook(update, hk_jikkyo_update_tag_race as *mut c_void);
    if tramp == 0 {
        return Err("failed to hook JikkyoTagProcessor.UpdateTagRace".into());
    }
    TAG_UPDATE_RACE_TRAMPOLINE.store(tramp, Ordering::Release);
    log(3, "hooked JikkyoTagProcessor..ctor / UpdateTagRace (big fields)");
    if let Err(e) = install_skill_pool_fix(get_image, get_class, get_addr) {
        log(1, &format!("big field: race-view skill fix not installed ({e})"));
    }
    if let Err(e) = install_effect_work_fix(get_image, get_class, get_addr) {
        log(1, &format!("big field: skill-effect fix not installed ({e})"));
    }
    if let Err(e) = install_ui_rank_fix(get_image, get_class, get_addr) {
        log(1, &format!("big field: race HUD rank fix not installed ({e})"));
    }
    if let Err(e) = install_minimap_fix(get_image, get_class, get_addr) {
        log(1, &format!("big field: minimap fix not installed ({e})"));
    }
    if let Err(e) = install_camera_fix(get_image, get_class, get_addr) {
        log(1, &format!("big field: camera fix not installed ({e})"));
    }
    Ok(())
}

const CODE_DUMP_BYTES: usize = 8192;
const CODE_DUMP_CLASSES: &[&CStr] = &[
    c"HorseRaceInfo",
    c"JikkyoTagProcessor",
    c"SkillEffectPool",
    c"RaceHorseManagerBase",
    c"RaceHorseManagerReplay",
    c"HorseData",
    c"SkillBase",
    c"RaceManager",
    c"RaceManagerReplayBase",
    c"RaceManagerReplay",
    c"RaceViewBase",
    c"RaceViewReplay",
    c"IHorseRaceInfo",
    c"IRaceHorseAccessor",
    c"HorseRaceInfoReplay",
    c"SkillManager",
];

fn dump_classes() {
    let Some(api) = API.get() else { return };
    let (Some(get_image), Some(get_class)) = (api.get_image, api.get_class) else { return };
    let uma = unsafe { get_image(c"umamusume.dll".as_ptr()) };
    if uma.is_null() {
        return;
    }
    for &name in CODE_DUMP_CLASSES {
        let class = unsafe { get_class(uma, c"Gallop".as_ptr(), name.as_ptr()) };
        if !class.is_null() {
            let _ = std::panic::catch_unwind(|| dump_class_code(class, &name.to_string_lossy()));
        }
    }
}

fn dump_class_code(klass: *mut c_void, name: &str) {
    type GetMethods = unsafe extern "C" fn(*mut c_void, *mut *mut c_void) -> *const c_void;
    type GetFields = unsafe extern "C" fn(*mut c_void, *mut *mut c_void) -> *mut c_void;
    type NameOf = unsafe extern "C" fn(*const c_void) -> *const c_char;
    type FieldOffset = unsafe extern "C" fn(*mut c_void) -> usize;
    type ParamCount = unsafe extern "C" fn(*const c_void) -> u32;
    let (Some(methods), Some(fields), Some(method_name), Some(field_name), Some(field_offset), Some(param_count)) = (
        il2cpp::<GetMethods>("il2cpp_class_get_methods"),
        il2cpp::<GetFields>("il2cpp_class_get_fields"),
        il2cpp::<NameOf>("il2cpp_method_get_name"),
        il2cpp::<NameOf>("il2cpp_field_get_name"),
        il2cpp::<FieldOffset>("il2cpp_field_get_offset"),
        il2cpp::<ParamCount>("il2cpp_method_get_param_count"),
    ) else {
        log(2, "code dump: il2cpp reflection exports unavailable");
        return;
    };
    let Some(dir) = config_path().and_then(|p| p.parent().map(|d| d.join("clientlog"))) else { return };
    let _ = std::fs::create_dir_all(&dir);
    let text = |p: *const c_char| if p.is_null() { String::new() } else { unsafe { CStr::from_ptr(p) }.to_string_lossy().into_owned() };
    let base = export_module_base("GameAssembly.dll");
    let mut out = format!("{name}\nGameAssembly base {base:#x}\n\nfields:\n");
    unsafe {
        let mut iter: *mut c_void = std::ptr::null_mut();
        loop {
            let f = fields(klass, &mut iter);
            if f.is_null() {
                break;
            }
            out += &format!("  {:#06x} {}\n", field_offset(f), text(field_name(f)));
        }
        out += "\nmethods:\n";
        let mut iter: *mut c_void = std::ptr::null_mut();
        loop {
            let m = methods(klass, &mut iter);
            if m.is_null() {
                break;
            }
            let addr = *(m as *const usize);
            let mname = text(method_name(m));
            let argc = param_count(m);
            out += &format!("  {addr:#x} (rva {:#x}) {mname}/{argc}\n", addr.wrapping_sub(base));
            if addr != 0 && writable_or_code(addr as *const u8, CODE_DUMP_BYTES) {
                let bytes = std::slice::from_raw_parts(addr as *const u8, CODE_DUMP_BYTES);
                let safe: String = mname.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '_' }).collect();
                let _ = std::fs::write(dir.join(format!("{name}.{safe}_{argc}.bin")), bytes);
            }
        }
    }
    let path = dir.join(format!("{name}.txt"));
    let _ = std::fs::write(&path, out);
    log(3, &format!("code dump: {}", path.display()));
}

fn export_module_base(module: &str) -> usize {
    let w = wide(module);
    unsafe { GetModuleHandleW(w.as_ptr()) as usize }
}

fn writable_or_code(p: *const u8, len: usize) -> bool {
    let mut a = p as usize;
    let end = a + len;
    while a < end {
        let mut mbi = MemoryBasicInformation::default();
        let n = unsafe { VirtualQuery(a as *const c_void, &mut mbi, std::mem::size_of::<MemoryBasicInformation>()) };
        if n == 0 || mbi.state != 0x1000 || mbi.protect & 0x100 != 0 || mbi.protect & 0x01 != 0 || mbi.protect == 0 {
            return false;
        }
        a = mbi.base + mbi.region_size;
    }
    true
}

static CODE_DUMP_STATE: Mutex<String> = Mutex::new(String::new());
static CODE_DUMP_RUNNING: AtomicBool = AtomicBool::new(false);

fn set_code_dump_state(s: String) {
    *CODE_DUMP_STATE.lock().unwrap_or_else(|e| e.into_inner()) = s;
}

fn start_code_dump() {
    if CODE_DUMP_RUNNING.swap(true, Ordering::AcqRel) {
        return;
    }
    set_code_dump_state("dumping...".into());
    std::thread::spawn(|| {
        let result = std::panic::catch_unwind(|| unsafe { dump_game_code() });
        let msg = match result {
            Ok(Ok(m)) => m,
            Ok(Err(e)) => format!("failed: {e}"),
            Err(_) => "failed: panicked".into(),
        };
        log(3, &format!("code dump {msg}"));
        set_code_dump_state(msg);
        CODE_DUMP_RUNNING.store(false, Ordering::Release);
    });
}

unsafe fn dump_game_code() -> Result<String, String> {
    type DomainGet = unsafe extern "C" fn() -> *mut c_void;
    type ThreadAttach = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type ThreadDetach = unsafe extern "C" fn(*mut c_void);
    type GetAssemblies = unsafe extern "C" fn(*mut c_void, *mut usize) -> *const *mut c_void;
    type AssemblyImage = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type ImageName = unsafe extern "C" fn(*mut c_void) -> *const c_char;
    type ClassCount = unsafe extern "C" fn(*mut c_void) -> usize;
    type ImageClass = unsafe extern "C" fn(*mut c_void, usize) -> *mut c_void;
    type ClassText = unsafe extern "C" fn(*mut c_void) -> *const c_char;
    type DeclaringType = unsafe extern "C" fn(*mut c_void) -> *mut c_void;
    type GetMethods = unsafe extern "C" fn(*mut c_void, *mut *mut c_void) -> *const c_void;
    type GetFields = unsafe extern "C" fn(*mut c_void, *mut *mut c_void) -> *mut c_void;
    type NameOf = unsafe extern "C" fn(*const c_void) -> *const c_char;
    type FieldName = unsafe extern "C" fn(*mut c_void) -> *const c_char;
    type FieldOffset = unsafe extern "C" fn(*mut c_void) -> usize;
    type FieldFlags = unsafe extern "C" fn(*mut c_void) -> i32;
    type ParamCount = unsafe extern "C" fn(*const c_void) -> u32;

    let missing = |n: &str| format!("il2cpp export {n} unavailable");
    macro_rules! export {
        ($t:ty, $n:literal) => {
            il2cpp::<$t>($n).ok_or_else(|| missing($n))?
        };
    }
    let domain_get = export!(DomainGet, "il2cpp_domain_get");
    let attach = export!(ThreadAttach, "il2cpp_thread_attach");
    let detach = export!(ThreadDetach, "il2cpp_thread_detach");
    let assemblies = export!(GetAssemblies, "il2cpp_domain_get_assemblies");
    let assembly_image = export!(AssemblyImage, "il2cpp_assembly_get_image");
    let image_name = export!(ImageName, "il2cpp_image_get_name");
    let class_count = export!(ClassCount, "il2cpp_image_get_class_count");
    let image_class = export!(ImageClass, "il2cpp_image_get_class");
    let class_name = export!(ClassText, "il2cpp_class_get_name");
    let class_ns = export!(ClassText, "il2cpp_class_get_namespace");
    let declaring = export!(DeclaringType, "il2cpp_class_get_declaring_type");
    let methods = export!(GetMethods, "il2cpp_class_get_methods");
    let fields = export!(GetFields, "il2cpp_class_get_fields");
    let method_name = export!(NameOf, "il2cpp_method_get_name");
    let param_count = export!(ParamCount, "il2cpp_method_get_param_count");
    let field_name = export!(FieldName, "il2cpp_field_get_name");
    let field_offset = export!(FieldOffset, "il2cpp_field_get_offset");
    let field_flags = export!(FieldFlags, "il2cpp_field_get_flags");

    let dir = config_path()
        .and_then(|p| p.parent().map(|d| d.join("codedump")))
        .ok_or("no hachimi dir")?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let base = export_module_base("GameAssembly.dll");
    if base == 0 {
        return Err("GameAssembly.dll not loaded".into());
    }

    let thread = attach(domain_get());
    let text = |p: *const c_char| if p.is_null() { String::new() } else { CStr::from_ptr(p).to_string_lossy().into_owned() };
    let full_name = |k: *mut c_void| {
        let mut parts = vec![text(class_name(k))];
        let mut outer = declaring(k);
        let mut ns = text(class_ns(k));
        while !outer.is_null() {
            parts.push(text(class_name(outer)));
            ns = text(class_ns(outer));
            outer = declaring(outer);
        }
        parts.reverse();
        let nested = parts.join("/");
        if ns.is_empty() { nested } else { format!("{ns}.{nested}") }
    };

    let mut mtsv = String::from("rva\timage\tclass\tmethod\tparams\tslot\n");
    let mut ftsv = String::from("image\tclass\tfield\toffset\tstatic\n");
    let (mut n_methods, mut n_classes) = (0usize, 0usize);
    let mut count = 0usize;
    let list = assemblies(domain_get(), &mut count);
    for a in 0..count {
        let image = assembly_image(*list.add(a));
        if image.is_null() {
            continue;
        }
        let iname = text(image_name(image));
        for c in 0..class_count(image) {
            let k = image_class(image, c);
            if k.is_null() {
                continue;
            }
            n_classes += 1;
            let cname = full_name(k);
            let mut iter: *mut c_void = std::ptr::null_mut();
            let mut slot = 0;
            loop {
                let m = methods(k, &mut iter);
                if m.is_null() {
                    break;
                }
                let addr = *(m as *const usize);
                let rva = if addr == 0 { String::new() } else { format!("{:#x}", addr.wrapping_sub(base)) };
                mtsv += &format!("{rva}\t{iname}\t{cname}\t{}\t{}\t{slot}\n", text(method_name(m)), param_count(m));
                slot += 1;
                n_methods += 1;
            }
            let mut iter: *mut c_void = std::ptr::null_mut();
            loop {
                let f = fields(k, &mut iter);
                if f.is_null() {
                    break;
                }
                let is_static = field_flags(f) & 0x10 != 0;
                ftsv += &format!("{iname}\t{cname}\t{}\t{:#x}\t{}\n", text(field_name(f)), field_offset(f), is_static as u8);
            }
        }
    }
    if !thread.is_null() {
        detach(thread);
    }
    std::fs::write(dir.join("methods.tsv"), mtsv).map_err(|e| e.to_string())?;
    std::fs::write(dir.join("fields.tsv"), ftsv).map_err(|e| e.to_string())?;

    let nt = base + *((base + 0x3C) as *const u32) as usize;
    let size = *((nt + 0x50) as *const u32) as usize;
    let mut image = vec![0u8; size];
    let mut off = 0usize;
    while off < size {
        let mut mbi = MemoryBasicInformation::default();
        let n = VirtualQuery((base + off) as *const c_void, &mut mbi, std::mem::size_of::<MemoryBasicInformation>());
        if n == 0 {
            break;
        }
        let end = (mbi.base + mbi.region_size).min(base + size);
        let readable = mbi.state == 0x1000 && mbi.protect & 0x100 == 0 && mbi.protect & 0x01 == 0 && mbi.protect != 0;
        if readable {
            std::ptr::copy_nonoverlapping((base + off) as *const u8, image.as_mut_ptr().add(off), end - (base + off));
        }
        off = end - base;
    }
    std::fs::write(dir.join("GameAssembly.mem"), &image).map_err(|e| e.to_string())?;
    Ok(format!("done: {n_classes} classes, {n_methods} methods, {} MB image -> {}", size >> 20, dir.display()))
}

struct UiState {
    enabled: bool,
    buf: [u8; 256],
    error: Option<String>,
}

static UI: Mutex<UiState> = Mutex::new(UiState { enabled: false, buf: [0; 256], error: None });

impl UiState {
    fn reset_from_config(&mut self) {
        let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
        self.enabled = cfg.enabled;
        self.buf = [0; 256];
        let b = cfg.server.as_bytes();
        let n = b.len().min(self.buf.len() - 1);
        self.buf[..n].copy_from_slice(&b[..n]);
    }

    fn text(&self) -> String {
        let end = self.buf.iter().position(|&c| c == 0).unwrap_or(self.buf.len());
        String::from_utf8_lossy(&self.buf[..end]).into_owned()
    }
}

struct Ui<'a> {
    api: &'a Api,
    ui: *mut c_void,
}

impl Ui<'_> {
    fn text(&self, f: Option<UiTextFn>, s: &str) -> bool {
        let Some(f) = f else { return false };
        let c = cstr(s);
        unsafe { f(self.ui, c.as_ptr()) }
    }
    fn heading(&self, s: &str) {
        self.text(self.api.heading, s);
    }
    fn label(&self, s: &str) {
        self.text(self.api.label, s);
    }
    fn small(&self, s: &str) {
        self.text(self.api.small.or(self.api.label), s);
    }
    fn button(&self, s: &str) -> bool {
        self.text(self.api.button, s)
    }
    fn separator(&self) {
        if let Some(f) = self.api.separator {
            unsafe { f(self.ui) };
        }
    }
    fn colored(&self, (r, g, b): (u8, u8, u8), s: &str) {
        match self.api.colored {
            Some(f) => {
                let c = cstr(s);
                unsafe { f(self.ui, r, g, b, 255, c.as_ptr()) };
            }
            None => self.label(s),
        }
    }
}

const GREEN: (u8, u8, u8) = (120, 200, 120);
const AMBER: (u8, u8, u8) = (230, 180, 80);
const RED: (u8, u8, u8) = (230, 100, 100);

extern "C" fn menu_section(ui: *mut c_void, _: *mut c_void) {
    let _ = std::panic::catch_unwind(|| draw(ui));
}

fn draw(ui: *mut c_void) {
    let Some(api) = API.get() else { return };
    let u = Ui { api, ui };
    let mut st = UI.lock().unwrap_or_else(|e| e.into_inner());

    u.heading("Server redirect");

    if let Some(cb) = api.checkbox {
        let label = cstr("Use custom server");
        let before = st.enabled;
        unsafe { cb(ui, label.as_ptr(), &mut st.enabled) };
        if st.enabled != before {
            let want = st.enabled;
            let address = st.text();
            drop(st);
            toggle(want, &address);
            st = UI.lock().unwrap_or_else(|e| e.into_inner());
        }
    }

    u.label("Server address");
    if let Some(te) = api.text_edit {
        let len = st.buf.len();
        unsafe { te(ui, st.buf.as_mut_ptr() as *mut c_char, len) };
        st.buf[len - 1] = 0;
    }
    let typed = st.text();
    let (active, enabled) = {
        let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
        (cfg.server.clone(), cfg.enabled)
    };
    let pending = normalize(&typed).map(|(s, _)| s != active).unwrap_or(true);
    if pending && u.button("Apply") {
        match normalize(&typed) {
            Ok((server, host)) => {
                st.error = None;
                drop(st);
                set_config(enabled, server.clone(), host, true);
                notify(&format!("Server address set to {server}"));
                st = UI.lock().unwrap_or_else(|e| e.into_inner());
            }
            Err(e) => st.error = Some(e),
        }
    }
    if let Some(e) = &st.error {
        u.colored(RED, e);
    }
    drop(st);

    if let Some(cb) = api.checkbox {
        let label = cstr("Enable Admin Panel support for future Global content");
        let mut on = FUTURE_CONTENT.load(Ordering::Acquire);
        unsafe { cb(ui, label.as_ptr(), &mut on) };
        if on != FUTURE_CONTENT.load(Ordering::Acquire) {
            FUTURE_CONTENT.store(on, Ordering::Release);
            save_config(&CONFIG.read().unwrap_or_else(|e| e.into_inner()));
        }
    }

    if region() == Region::Jp {
        if let Some(cb) = api.checkbox {
            let label = cstr("Capture API logs on the official server");
            let mut on = CAPTURE.load(Ordering::Acquire);
            unsafe { cb(ui, label.as_ptr(), &mut on) };
            if on != CAPTURE.load(Ordering::Acquire) {
                CAPTURE.store(on, Ordering::Release);
                save_config(&CONFIG.read().unwrap_or_else(|e| e.into_inner()));
            }
        }
    }

    if let Some(cb) = api.checkbox {
        let label = cstr("Allow races with more than 18 runners");
        let mut on = BIG_FIELDS.load(Ordering::Acquire);
        unsafe { cb(ui, label.as_ptr(), &mut on) };
        if on != BIG_FIELDS.load(Ordering::Acquire) {
            BIG_FIELDS.store(on, Ordering::Release);
            if on && MANAGED_HOOKS.load(Ordering::Acquire) {
                let _ = std::panic::catch_unwind(try_install_big_fields);
            }
            save_config(&CONFIG.read().unwrap_or_else(|e| e.into_inner()));
        }
    }

    if let Some(cb) = api.checkbox {
        let label = cstr("Write the game's error log (debugging)");
        let mut on = CLIENT_LOG.load(Ordering::Acquire);
        unsafe { cb(ui, label.as_ptr(), &mut on) };
        if on != CLIENT_LOG.load(Ordering::Acquire) {
            set_client_log(on);
            save_config(&CONFIG.read().unwrap_or_else(|e| e.into_inner()));
        }
    }

    if u.button("Dump game code (debugging)") {
        start_code_dump();
    }
    let dump_state = CODE_DUMP_STATE.lock().unwrap_or_else(|e| e.into_inner()).clone();
    if !dump_state.is_empty() {
        u.small(&format!("Code dump: {dump_state}"));
    }

    u.separator();
    if BIG_FIELDS.load(Ordering::Acquire) && BIG_FIELDS_INSTALLED.get() == Some(&false) {
        u.colored(RED, "Could not install the 18+ runner fix (see hachimi.log)");
    }
    if !BIG_FIELDS.load(Ordering::Acquire) && BIG_FIELDS_INSTALLED.get() == Some(&true) {
        u.small("18+ runner hooks stay placed (inactive) until the game restarts");
    }
    if CLIENT_LOG.load(Ordering::Acquire) {
        if CLIENT_LOG_INSTALLED.get() == Some(&false) {
            u.colored(RED, "Could not hook the game's logging (see hachimi.log)");
        } else {
            let path = CLIENT_LOG_PATH.lock().unwrap_or_else(|e| e.into_inner()).clone();
            let n = CLIENT_LOGGED.load(Ordering::Relaxed);
            u.colored(GREEN, &format!("Client log: {n} entries{}", if path.is_empty() { String::new() } else { format!(" -> {path}") }));
        }
    }
    if region() == Region::Jp && CAPTURE.load(Ordering::Acquire) {
        if enabled {
            u.small("Capture: paused while using the custom server");
        } else {
            let path = CAPTURE_FILE.lock().unwrap_or_else(|e| e.into_inner()).path.clone();
            let n = CAPTURED.load(Ordering::Relaxed);
            u.colored(GREEN, &format!("Capturing: {n} records{}", if path.is_empty() { String::new() } else { format!(" -> {path}") }));
            if !REPLY_HOOKED.load(Ordering::Acquire) && *SEAL_STATE.lock().unwrap_or_else(|e| e.into_inner()) == "ready" {
                u.colored(AMBER, "Replies are not being captured (see hachimi.log)");
            }
        }
    }
    if enabled {
        let n = REDIRECTED.load(Ordering::Relaxed);
        u.colored(GREEN, &format!("On: {active} ({n} requests)"));
    } else {
        u.small("Off: using the official server");
    }
    if POST_INSTALLED.get() == Some(&false) {
        u.colored(RED, "Could not hook the game's requests (see hachimi.log)");
    }
    if CERT_HOOKED.get() == Some(&false) {
        u.colored(RED, "Could not hook certificate checks; the server's certificate must be trusted");
    }
    if CENSOR.load(Ordering::Acquire) && CENSOR_INSTALLED.get() == Some(&false) {
        u.colored(RED, "Could not hide device/Steam info (see hachimi.log)");
    }
    if region() == Region::Jp && enabled {
        let seal = *SEAL_STATE.lock().unwrap_or_else(|e| e.into_inner());
        let armed = ARMED.lock().unwrap_or_else(|e| e.into_inner()).as_ref().map(|a| a.key.is_some());
        if seal.starts_with("blocked") || seal.starts_with("failed") {
            u.colored(RED, &format!("Encryption: {seal}"));
        } else if armed == Some(false) {
            u.colored(AMBER, "Encryption: server's key channel (port 8090) not answering");
        }
    }
    custom_support::menu(&u);
    umapack::menu(&u);
}

fn toggle(want: bool, address: &str) {
    if !want {
        let cfg = CONFIG.read().unwrap_or_else(|e| e.into_inner());
        let (server, host) = (cfg.server.clone(), cfg.host.clone());
        drop(cfg);
        set_config(false, server, host, true);
        log(3, "redirect turned OFF");
        notify("Server redirect off: the official server is used");
        return;
    }
    match normalize(address) {
        Ok((server, host)) => {
            set_config(true, server.clone(), host, true);
            log(3, &format!("redirect turned ON -> {server}"));
            notify(&format!("Server redirect on: {server}"));
        }
        Err(e) => {
            let mut st = UI.lock().unwrap_or_else(|e| e.into_inner());
            st.enabled = false;
            st.error = Some(e);
        }
    }
}
