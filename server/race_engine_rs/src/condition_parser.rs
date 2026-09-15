//! Port of condition_parser.py.
//!
//! The original is a Pratt (TDOP) parser purely because the upstream
//! TypeScript engine was written that way; the grammar it implements has no
//! parentheses and only three precedence levels, which a plain recursive
//! descent parser expresses directly and equivalently:
//!
//!   Or  ::= And '@' Or | And
//!   And ::= Cmp '&' And | Cmp
//!   Cmp ::= condition Op integer
//!
//! Every Parser this codebase ever constructs uses the stock And/Or/
//! comparison operators (only the *condition table* is ever swapped, for
//! `.withActivateCountsAsRandom()`), so unlike the Python port this file
//! does not reproduce the swappable-operator-class machinery -- there is
//! nothing here that would ever exercise it.

use crate::activation_conditions::{make_and, make_or, CmpKind, CondResult, Condition, Op};
use std::collections::HashMap;

#[derive(Clone)]
enum Token {
    Int(i64),
    Ident(String),
    Eq,
    Neq,
    Lt,
    Lte,
    Gt,
    Gte,
    And,
    Or,
}

fn is_id_char(c: char) -> bool {
    c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_'
}

fn tokenize(s: &str) -> CondResult<Vec<Token>> {
    let chars: Vec<char> = s.chars().collect();
    let n = chars.len();
    let mut i = 0usize;
    let mut out = Vec::new();
    while i < n {
        let ch = chars[i];
        if ch.is_ascii_digit() {
            let start = i;
            while i < n && chars[i].is_ascii_digit() {
                i += 1;
            }
            let text: String = chars[start..i].iter().collect();
            out.push(Token::Int(text.parse().map_err(|_| "bad integer literal".to_string())?));
        } else if is_id_char(ch) {
            let start = i;
            while i < n && is_id_char(chars[i]) {
                i += 1;
            }
            out.push(Token::Ident(chars[start..i].iter().collect()));
        } else if ch == '=' {
            if i + 1 >= n || chars[i + 1] != '=' {
                return Err("expected =".to_string());
            }
            i += 2;
            out.push(Token::Eq);
        } else if ch == '!' {
            if i + 1 >= n || chars[i + 1] != '=' {
                return Err("expected =".to_string());
            }
            i += 2;
            out.push(Token::Neq);
        } else if ch == '<' {
            if i + 1 < n && chars[i + 1] == '=' {
                i += 2;
                out.push(Token::Lte);
            } else {
                i += 1;
                out.push(Token::Lt);
            }
        } else if ch == '>' {
            if i + 1 < n && chars[i + 1] == '=' {
                i += 2;
                out.push(Token::Gte);
            } else {
                i += 1;
                out.push(Token::Gt);
            }
        } else if ch == '@' {
            i += 1;
            out.push(Token::Or);
        } else if ch == '&' {
            i += 1;
            out.push(Token::And);
        } else {
            return Err(format!("invalid character {ch:?}"));
        }
    }
    Ok(out)
}

struct TokStream {
    toks: Vec<Token>,
    pos: usize,
}

impl TokStream {
    fn peek(&self) -> Option<&Token> {
        self.toks.get(self.pos)
    }

    fn advance(&mut self) -> Option<Token> {
        let t = self.toks.get(self.pos).cloned();
        if t.is_some() {
            self.pos += 1;
        }
        t
    }
}

fn parse_or(ts: &mut TokStream, conditions: &'static HashMap<String, Condition>) -> CondResult<Op> {
    let left = parse_and(ts, conditions)?;
    if matches!(ts.peek(), Some(Token::Or)) {
        ts.advance();
        let right = parse_or(ts, conditions)?;
        make_or(left, right)
    } else {
        Ok(left)
    }
}

fn parse_and(ts: &mut TokStream, conditions: &'static HashMap<String, Condition>) -> CondResult<Op> {
    let left = parse_cmp(ts, conditions)?;
    if matches!(ts.peek(), Some(Token::And)) {
        ts.advance();
        let right = parse_and(ts, conditions)?;
        make_and(left, right)
    } else {
        Ok(left)
    }
}

fn parse_cmp(ts: &mut TokStream, conditions: &'static HashMap<String, Condition>) -> CondResult<Op> {
    let name = match ts.advance() {
        Some(Token::Ident(s)) => s,
        _ => return Err("expected condition identifier".to_string()),
    };
    let cond: &'static Condition = crate::activation_conditions::lookup_condition(conditions, &name)
        .ok_or_else(|| format!("unknown condition {name}"))?;
    let kind = match ts.advance() {
        Some(Token::Eq) => CmpKind::Eq,
        Some(Token::Neq) => CmpKind::Neq,
        Some(Token::Lt) => CmpKind::Lt,
        Some(Token::Lte) => CmpKind::Lte,
        Some(Token::Gt) => CmpKind::Gt,
        Some(Token::Gte) => CmpKind::Gte,
        _ => return Err("expected comparison operator".to_string()),
    };
    let arg = match ts.advance() {
        Some(Token::Int(n)) => n as f64,
        _ => return Err("expected integer".to_string()),
    };
    Ok(Op::Cmp { cond, kind, arg })
}

/// Parses one condition-expression string (e.g. `"phase==2&distance_rate>=50"`)
/// against the given condition table (`Conditions` or
/// `ConditionsWithActivateCountsAsRandom`).
pub fn parse(conditions: &'static HashMap<String, Condition>, s: &str) -> CondResult<Op> {
    let toks = tokenize(s)?;
    if toks.is_empty() {
        return Err("empty condition expression".to_string());
    }
    let mut ts = TokStream { toks, pos: 0 };
    let op = parse_or(&mut ts, conditions)?;
    if ts.pos != ts.toks.len() {
        return Err("trailing tokens after expression".to_string());
    }
    Ok(op)
}
