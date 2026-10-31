"""Forwarded capture values with writable projections.

Flow rewrites composite assignment destinations before passing them to Amaranth.
Outside Flow, composite destinations must explicitly use the backing registers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum
from typing import Any, cast, override

from amaranth import Const, Mux, ShapeCastable, Signal, Value, ValueCastable
from amaranth.hdl._ast import (
  Assign,
  Concat,
  Operator,
  Part,
  Slice,
  Statement,
  SwitchValue,
)
from amaranth.hdl._xfrm import StatementTransformer


def _call(target: object, name: str, *args: object, **kwargs: object) -> object:
  method = cast(Callable[..., object], getattr(target, name))
  return method(*args, **kwargs)


def _binary(name: str) -> Callable[[_Proxy, object], Value]:
  def method(self: _Proxy, other: object) -> Value:
    if isinstance(other, _Proxy):
      other = other.value
    return cast(Value, _call(self.value, name, other))
  return method


def _unary(name: str) -> Callable[[_Proxy], Value]:
  def method(self: _Proxy) -> Value:
    return cast(Value, _call(self.value, name))
  return method


class _Proxy(ValueCastable):
  def __init__(self, read: Value | ValueCastable, write: Value | ValueCastable):
    super().__init__()
    self._read = read
    self._write = write
    self._raw = Value.cast(read)
    self._shape = read.shape()

  @override
  def as_value(self) -> Value:
    return self._raw

  @override
  def shape(self):
    return self._shape

  @property
  def value(self) -> Value | ValueCastable:
    return self._read

  @property
  def reg(self) -> Value | ValueCastable:
    return self._write

  def eq(self, value: object, *, src_loc_at: int = 0) -> Statement:
    return Value.cast(self._write).eq(value, src_loc_at=src_loc_at + 1)

  def __getitem__(self, key: object) -> _Proxy:
    read = cast(Value | ValueCastable, _call(self._read, "__getitem__", key))
    write = cast(Value | ValueCastable, _call(self._write, "__getitem__", key))
    return _Proxy(read, write)

  def __getattr__(self, name: str) -> Any:  # pyright: ignore[reportExplicitAny]
    if name.startswith("_"):
      raise AttributeError(name)
    read = getattr(self._read, name)
    if isinstance(read, (Value, ValueCastable)):
      write = cast(Value | ValueCastable, getattr(self._write, name))
      return _Proxy(read, write)
    if callable(read):
      def method(*args: object, **kwargs: object) -> object:
        result = _call(self._read, name, *args, **kwargs)
        if isinstance(result, (Value, ValueCastable)):
          write = cast(
            Value | ValueCastable,
            _call(self._write, name, *args, **kwargs),
          )
          return _Proxy(result, write)
        return result
      return method
    return read

  def as_signed(self) -> _Proxy:
    return _Proxy(self._raw.as_signed(), Value.cast(self._write).as_signed())

  def as_unsigned(self) -> _Proxy:
    return _Proxy(self._raw.as_unsigned(), Value.cast(self._write).as_unsigned())

  def bit_select(self, offset: object, width: int) -> _Proxy:
    return _Proxy(
      self._raw.bit_select(offset, width),
      Value.cast(self._write).bit_select(offset, width),
    )

  def word_select(self, offset: object, width: int) -> _Proxy:
    return _Proxy(
      self._raw.word_select(offset, width),
      Value.cast(self._write).word_select(offset, width),
    )

  def __len__(self) -> int:
    return cast(int, _call(self._read, "__len__"))

  def __bool__(self) -> bool:
    return bool(self._raw)

  def __contains__(self, value: object) -> bool:
    return cast(bool, _call(self._read, "__contains__", value))

  @override
  def __format__(self, spec: str) -> str:
    return format(self._read, spec)

  @override
  def __repr__(self) -> str:
    return f"Captured({self._read!r})"

  __pos__ = _unary("__pos__")
  __neg__ = _unary("__neg__")
  __abs__ = _unary("__abs__")
  __invert__ = _unary("__invert__")
  __add__ = _binary("__add__")
  __radd__ = _binary("__radd__")
  __sub__ = _binary("__sub__")
  __rsub__ = _binary("__rsub__")
  __mul__ = _binary("__mul__")
  __rmul__ = _binary("__rmul__")
  __floordiv__ = _binary("__floordiv__")
  __rfloordiv__ = _binary("__rfloordiv__")
  __mod__ = _binary("__mod__")
  __rmod__ = _binary("__rmod__")
  __and__ = _binary("__and__")
  __rand__ = _binary("__rand__")
  __or__ = _binary("__or__")
  __ror__ = _binary("__ror__")
  __xor__ = _binary("__xor__")
  __rxor__ = _binary("__rxor__")
  __lshift__ = _binary("__lshift__")
  __rlshift__ = _binary("__rlshift__")
  __rshift__ = _binary("__rshift__")
  __rrshift__ = _binary("__rrshift__")
  # Like Amaranth values, comparisons construct hardware rather than Python bools.
  __eq__ = _binary("__eq__")  # pyright: ignore[reportAssignmentType]
  __ne__ = _binary("__ne__")  # pyright: ignore[reportAssignmentType]
  __lt__ = _binary("__lt__")
  __le__ = _binary("__le__")
  __gt__ = _binary("__gt__")
  __ge__ = _binary("__ge__")


class Capture(_Proxy):
  """Read the forwarded source, but direct assignments to captured storage."""

  def __init__(self, value: Value | ValueCastable | Enum | int):
    self.source = value
    self.bypass = Signal()
    reg = Signal.like(value)
    forwarded = Mux(self.bypass, value, reg)
    # This read marker must not be assignable before Flow rewrites it. Otherwise
    # an escaped composite assignment could write the source through the mux.
    forwarded = forwarded ^ Const(0, forwarded.shape())
    shape = reg.shape()
    read = shape(forwarded) if isinstance(shape, ShapeCastable) else forwarded
    super().__init__(read, reg)


class CaptureWrites(StatementTransformer):
  """Replace capture read markers only along writable assignment paths.

  This uses Amaranth 0.5's internal AST to support native Cat/Array destinations
  without changing their RHS, dynamic indices, or switch selectors.
  """

  def __init__(self, captures: Iterable[Capture]):
    self._registers = {
      id(captured.as_value()): (captured.as_value(), Value.cast(captured.reg))
      for captured in captures
    }

  @override
  def on_Assign(self, stmt: Assign) -> Assign:
    lhs = self._lhs(stmt.lhs)
    if lhs is stmt.lhs:
      return stmt
    result = Assign(lhs, stmt.rhs)
    result.src_loc = stmt.src_loc
    return result

  def _lhs(self, value: Value) -> Value:
    registered = self._registers.get(id(value))
    if registered is not None:
      return registered[1]

    if isinstance(value, Slice):
      result = Slice(self._lhs(value.value), value.start, value.stop)
    elif isinstance(value, Part):
      result = Part(self._lhs(value.value), value.offset, value.width, value.stride)
    elif isinstance(value, Concat):
      result = Concat(self._lhs(part) for part in value.parts)
    elif isinstance(value, Operator) and value.operator in ("u", "s"):
      result = Operator(value.operator, [self._lhs(part) for part in value.operands])
    elif isinstance(value, SwitchValue):
      result = SwitchValue(value.test, [
        (patterns, self._lhs(part)) for patterns, part in value.cases
      ])
    else:
      return value

    result.src_loc = value.src_loc
    return result
