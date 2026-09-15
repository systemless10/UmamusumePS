"""Port of ConditionParser.ts -- tokenizer + Pratt (top-down operator
precedence) parser for skill activation-condition strings.

Grammar (no parentheses, no other precedence control):
    Or  ::= And '@' Or | And
    And ::= Cmp '&' And | Cmp
    Cmp ::= condition Op integer
    Op  ::= '==' | '!=' | '>' | '>=' | '<' | '<='
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from .activation_conditions import (
    AndOperator, Condition, Conditions, EqOperator, GteOperator, GtOperator,
    LteOperator, LtOperator, NeqOperator, OrOperator,
)


class ParseError(Exception):
    pass


def _is_id_char(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_"


class NodeType(Enum):
    INT = auto()
    COND = auto()
    OP = auto()


@dataclass
class Node:
    type: NodeType
    value: Optional[float] = None
    cond: Optional[Condition] = None
    op: Optional[object] = None


class _Token:
    lbp = 0

    def led(self, state, left):
        raise ParseError("unexpected token")

    def nud(self, state):
        raise ParseError("unexpected token")


class _IntValue(_Token):
    lbp = 0

    def __init__(self, value: int):
        self.value = value

    def nud(self, state):
        return Node(NodeType.INT, value=self.value)


class _Eof(_Token):
    lbp = 0

    def led(self, state, left):
        raise ParseError("unexpected eof")

    def nud(self, state):
        raise ParseError("unexpected eof")


_EOF = _Eof()


class _CmpOp(_Token):
    def __init__(self, lbp: int, opclass):
        self.lbp = lbp
        self.opclass = opclass

    def led(self, state, left):
        if left.type != NodeType.COND:
            raise ParseError("expected condition on left hand side of comparison")
        right = _expression(state, self.lbp)
        if right.type != NodeType.INT:
            raise ParseError("expected number on right hand side of comparison")
        return Node(NodeType.OP, op=self.opclass(left.cond, right.value))

    def nud(self, state):
        raise ParseError("expected expression")


class _LogicalOp(_Token):
    def __init__(self, lbp: int, opclass):
        self.lbp = lbp
        self.opclass = opclass

    def led(self, state, left):
        if left.type != NodeType.OP:
            raise ParseError("expected comparison on left hand side of operator")
        right = _expression(state, self.lbp)
        if right.type != NodeType.OP:
            raise ParseError("expected comparison on right hand side of operator")
        return Node(NodeType.OP, op=self.opclass(left.op, right.op))

    def nud(self, state):
        raise ParseError("expected expression")


@dataclass
class _ParserState:
    current: _Token
    next: Optional[_Token]
    tokens: iter


def _expression(state: _ParserState, rbp: int) -> Node:
    state.current = state.next
    state.next = next(state.tokens, _EOF)
    left = state.current.nud(state)
    while rbp < state.next.lbp:
        state.current = state.next
        state.next = next(state.tokens, _EOF)
        left = state.current.led(state, left)
    return left


class Parser:
    """Bound to a specific condition table (default `Conditions`, or the
    `activate_count`-as-random variant for `withActivateCountsAsRandom()`)."""

    def __init__(self, conditions: dict[str, Condition] = None,
                 operators: Optional[dict] = None):
        self.conditions = conditions if conditions is not None else Conditions
        ops = operators or {}
        self._and = ops.get("and", AndOperator)
        self._or = ops.get("or", OrOperator)
        self._eq = ops.get("eq", EqOperator)
        self._neq = ops.get("neq", NeqOperator)
        self._lt = ops.get("lt", LtOperator)
        self._lte = ops.get("lte", LteOperator)
        self._gt = ops.get("gt", GtOperator)
        self._gte = ops.get("gte", GteOperator)

        self._op_eq = _CmpOp(30, self._eq)
        self._op_neq = _CmpOp(30, self._neq)
        self._op_lt = _CmpOp(30, self._lt)
        self._op_lte = _CmpOp(30, self._lte)
        self._op_gt = _CmpOp(30, self._gt)
        self._op_gte = _CmpOp(30, self._gte)
        self._op_and = _LogicalOp(20, self._and)
        self._op_or = _LogicalOp(10, self._or)

    class _Identifier(_Token):
        lbp = 0

        def __init__(self, value: str, conditions: dict):
            self.value = value
            self._conditions = conditions

        def nud(self, state):
            return Node(NodeType.COND, cond=self._conditions[self.value])

    def tokenize(self, s: str):
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch.isdigit():
                start = i
                while i < n and s[i].isdigit():
                    i += 1
                yield _IntValue(int(s[start:i]))
            elif _is_id_char(ch):
                start = i
                while i < n and _is_id_char(s[i]):
                    i += 1
                yield Parser._Identifier(s[start:i], self.conditions)
            elif ch == "=":
                if i + 1 >= n or s[i + 1] != "=":
                    raise ParseError("expected =")
                i += 2
                yield self._op_eq
            elif ch == "!":
                if i + 1 >= n or s[i + 1] != "=":
                    raise ParseError("expected =")
                i += 2
                yield self._op_neq
            elif ch == "<":
                if i + 1 < n and s[i + 1] == "=":
                    i += 2
                    yield self._op_lte
                else:
                    i += 1
                    yield self._op_lt
            elif ch == ">":
                if i + 1 < n and s[i + 1] == "=":
                    i += 2
                    yield self._op_gte
                else:
                    i += 1
                    yield self._op_gt
            elif ch == "@":
                i += 1
                yield self._op_or
            elif ch == "&":
                i += 1
                yield self._op_and
            else:
                raise ParseError("invalid character")

    def parse_any(self, tokens) -> Node:
        it = iter(tokens)
        state = _ParserState(current=_EOF, next=next(it, _EOF), tokens=it)
        return _expression(state, 0)

    def parse(self, tokens):
        node = self.parse_any(tokens)
        if node.type != NodeType.OP:
            raise ParseError("expected comparison or operator")
        return node.op


_default_parser: Optional[Parser] = None


def get_parser(conditions: dict[str, Condition] = None, operators: Optional[dict] = None) -> Parser:
    if conditions is None and operators is None:
        global _default_parser
        if _default_parser is None:
            _default_parser = Parser()
        return _default_parser
    return Parser(conditions, operators)
