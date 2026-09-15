//! Minimal hand-rolled JSON value parser/serializer -- just enough to load
//! course_data.json/skill_data.json and speak the race-spec stdin/stdout
//! contract, with zero external dependencies (so the final binary needs no
//! crates.io access to build, matching how the rest of this crate was kept
//! dependency-free while the build toolchain was still being set up).
//! Not a general-purpose JSON library: no streaming, no arbitrary-precision
//! numbers (all numbers are f64, which is exactly what every field in this
//! contract needs), no comments/trailing-comma leniency.

use std::collections::BTreeMap;
use std::fmt::Write as _;

#[derive(Clone, Debug)]
pub enum Value {
    Null,
    Bool(bool),
    Number(f64),
    String(String),
    Array(Vec<Value>),
    Object(BTreeMap<String, Value>),
}

impl Value {
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            Value::Number(n) => Some(*n),
            _ => None,
        }
    }
    pub fn as_i64(&self) -> Option<i64> {
        self.as_f64().map(|n| n as i64)
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::String(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_array(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Array(a) => Some(a),
            _ => None,
        }
    }
    pub fn as_object(&self) -> Option<&BTreeMap<String, Value>> {
        match self {
            Value::Object(o) => Some(o),
            _ => None,
        }
    }
    pub fn get(&self, key: &str) -> Option<&Value> {
        self.as_object().and_then(|o| o.get(key))
    }
    pub fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }

    pub fn get_f64(&self, key: &str) -> Result<f64, String> {
        self.get(key).and_then(Value::as_f64).ok_or_else(|| format!("missing/invalid numeric field {key:?}"))
    }
    pub fn get_i64(&self, key: &str) -> Result<i64, String> {
        self.get(key).and_then(Value::as_i64).ok_or_else(|| format!("missing/invalid integer field {key:?}"))
    }
    pub fn get_str<'a>(&'a self, key: &str) -> Result<&'a str, String> {
        self.get(key).and_then(Value::as_str).ok_or_else(|| format!("missing/invalid string field {key:?}"))
    }
    pub fn get_bool(&self, key: &str) -> Result<bool, String> {
        self.get(key).and_then(Value::as_bool).ok_or_else(|| format!("missing/invalid bool field {key:?}"))
    }
    pub fn get_array<'a>(&'a self, key: &str) -> Result<&'a Vec<Value>, String> {
        self.get(key).and_then(Value::as_array).ok_or_else(|| format!("missing/invalid array field {key:?}"))
    }
    pub fn get_object<'a>(&'a self, key: &str) -> Result<&'a BTreeMap<String, Value>, String> {
        self.get(key).and_then(Value::as_object).ok_or_else(|| format!("missing/invalid object field {key:?}"))
    }
}

// ---------------------------------------------------------------------------
// Parsing

pub fn parse(s: &str) -> Result<Value, String> {
    let chars: Vec<char> = s.chars().collect();
    let mut pos = 0usize;
    let v = parse_value(&chars, &mut pos)?;
    skip_ws(&chars, &mut pos);
    if pos != chars.len() {
        return Err(format!("trailing data at byte {pos}"));
    }
    Ok(v)
}

fn skip_ws(chars: &[char], pos: &mut usize) {
    while *pos < chars.len() && chars[*pos].is_whitespace() {
        *pos += 1;
    }
}

fn parse_value(chars: &[char], pos: &mut usize) -> Result<Value, String> {
    skip_ws(chars, pos);
    if *pos >= chars.len() {
        return Err("unexpected end of input".to_string());
    }
    match chars[*pos] {
        '{' => parse_object(chars, pos),
        '[' => parse_array(chars, pos),
        '"' => Ok(Value::String(parse_string(chars, pos)?)),
        't' => {
            expect_literal(chars, pos, "true")?;
            Ok(Value::Bool(true))
        }
        'f' => {
            expect_literal(chars, pos, "false")?;
            Ok(Value::Bool(false))
        }
        'n' => {
            expect_literal(chars, pos, "null")?;
            Ok(Value::Null)
        }
        c if c == '-' || c.is_ascii_digit() => parse_number(chars, pos),
        c => Err(format!("unexpected character {c:?} at byte {pos}")),
    }
}

fn expect_literal(chars: &[char], pos: &mut usize, lit: &str) -> Result<(), String> {
    for c in lit.chars() {
        if *pos >= chars.len() || chars[*pos] != c {
            return Err(format!("expected literal {lit:?} at byte {pos}"));
        }
        *pos += 1;
    }
    Ok(())
}

fn parse_object(chars: &[char], pos: &mut usize) -> Result<Value, String> {
    *pos += 1; // '{'
    let mut map = BTreeMap::new();
    skip_ws(chars, pos);
    if *pos < chars.len() && chars[*pos] == '}' {
        *pos += 1;
        return Ok(Value::Object(map));
    }
    loop {
        skip_ws(chars, pos);
        if *pos >= chars.len() || chars[*pos] != '"' {
            return Err(format!("expected object key at byte {pos}"));
        }
        let key = parse_string(chars, pos)?;
        skip_ws(chars, pos);
        if *pos >= chars.len() || chars[*pos] != ':' {
            return Err(format!("expected ':' at byte {pos}"));
        }
        *pos += 1;
        let value = parse_value(chars, pos)?;
        map.insert(key, value);
        skip_ws(chars, pos);
        if *pos >= chars.len() {
            return Err("unexpected end of input in object".to_string());
        }
        match chars[*pos] {
            ',' => {
                *pos += 1;
            }
            '}' => {
                *pos += 1;
                break;
            }
            c => return Err(format!("expected ',' or '}}' at byte {pos}, found {c:?}")),
        }
    }
    Ok(Value::Object(map))
}

fn parse_array(chars: &[char], pos: &mut usize) -> Result<Value, String> {
    *pos += 1; // '['
    let mut arr = Vec::new();
    skip_ws(chars, pos);
    if *pos < chars.len() && chars[*pos] == ']' {
        *pos += 1;
        return Ok(Value::Array(arr));
    }
    loop {
        let value = parse_value(chars, pos)?;
        arr.push(value);
        skip_ws(chars, pos);
        if *pos >= chars.len() {
            return Err("unexpected end of input in array".to_string());
        }
        match chars[*pos] {
            ',' => {
                *pos += 1;
            }
            ']' => {
                *pos += 1;
                break;
            }
            c => return Err(format!("expected ',' or ']' at byte {pos}, found {c:?}")),
        }
    }
    Ok(Value::Array(arr))
}

fn parse_string(chars: &[char], pos: &mut usize) -> Result<String, String> {
    *pos += 1; // opening quote
    let mut out = String::new();
    loop {
        if *pos >= chars.len() {
            return Err("unterminated string".to_string());
        }
        let c = chars[*pos];
        *pos += 1;
        match c {
            '"' => break,
            '\\' => {
                if *pos >= chars.len() {
                    return Err("unterminated escape".to_string());
                }
                let esc = chars[*pos];
                *pos += 1;
                match esc {
                    '"' => out.push('"'),
                    '\\' => out.push('\\'),
                    '/' => out.push('/'),
                    'n' => out.push('\n'),
                    't' => out.push('\t'),
                    'r' => out.push('\r'),
                    'b' => out.push('\u{8}'),
                    'f' => out.push('\u{c}'),
                    'u' => {
                        if *pos + 4 > chars.len() {
                            return Err("truncated \\u escape".to_string());
                        }
                        let hex: String = chars[*pos..*pos + 4].iter().collect();
                        let code = u32::from_str_radix(&hex, 16).map_err(|_| "invalid \\u escape".to_string())?;
                        *pos += 4;
                        if let Some(ch) = char::from_u32(code) {
                            out.push(ch);
                        }
                    }
                    other => return Err(format!("invalid escape \\{other}")),
                }
            }
            other => out.push(other),
        }
    }
    Ok(out)
}

fn parse_number(chars: &[char], pos: &mut usize) -> Result<Value, String> {
    let start = *pos;
    if chars[*pos] == '-' {
        *pos += 1;
    }
    while *pos < chars.len() && chars[*pos].is_ascii_digit() {
        *pos += 1;
    }
    if *pos < chars.len() && chars[*pos] == '.' {
        *pos += 1;
        while *pos < chars.len() && chars[*pos].is_ascii_digit() {
            *pos += 1;
        }
    }
    if *pos < chars.len() && (chars[*pos] == 'e' || chars[*pos] == 'E') {
        *pos += 1;
        if *pos < chars.len() && (chars[*pos] == '+' || chars[*pos] == '-') {
            *pos += 1;
        }
        while *pos < chars.len() && chars[*pos].is_ascii_digit() {
            *pos += 1;
        }
    }
    let text: String = chars[start..*pos].iter().collect();
    text.parse::<f64>().map(Value::Number).map_err(|_| format!("invalid number {text:?}"))
}

// ---------------------------------------------------------------------------
// Serializing

pub fn to_string(v: &Value) -> String {
    let mut out = String::new();
    write_value(&mut out, v);
    out
}

fn write_value(out: &mut String, v: &Value) {
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => write_number(out, *n),
        Value::String(s) => write_string(out, s),
        Value::Array(arr) => {
            out.push('[');
            for (i, item) in arr.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, k);
                out.push(':');
                write_value(out, val);
            }
            out.push('}');
        }
    }
}

fn write_number(out: &mut String, n: f64) {
    if n.is_nan() || n.is_infinite() {
        // Not valid JSON; every producer of these values in this codebase
        // guards against them reaching serialization, but fall back to
        // `null` rather than emit invalid JSON if one ever slips through.
        out.push_str("null");
    } else if n == n.trunc() && n.abs() < 1e15 {
        let _ = write!(out, "{}", n as i64);
    } else {
        let _ = write!(out, "{n}");
    }
}

fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// -- small builder helpers used by main.rs's response assembly -------------

pub fn obj(pairs: Vec<(&str, Value)>) -> Value {
    Value::Object(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

pub fn num(n: f64) -> Value {
    Value::Number(n)
}

pub fn arr(items: Vec<Value>) -> Value {
    Value::Array(items)
}
