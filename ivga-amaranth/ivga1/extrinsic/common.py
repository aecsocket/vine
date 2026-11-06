from collections.abc import Callable, Generator
from typing import Any

from amaranth import Signal
from amflow import Flow

from ivga1 import Port

type ExtFnMake = Callable[[ExtFnContext], ExtFnGen]
type ExtFnGen = Generator[object, Any, None] # pyright: ignore[reportExplicitAny]

class ExtTy:
  def __init__(self, name: str, *, is_copy: bool):
    self.name = name
    self.is_copy = is_copy

class ExtFn:
  def __init__(self, name: str, *, inputs: tuple[str, ...], fn: ExtFnMake):
    assert 1 <= len(inputs) <= 2
    self.name = name
    self.inputs = inputs
    self.fn = fn

class ExtFnContext:
  def __init__(
    self,
    f: Flow,
    heap,
    linker,
    a: Port,
    b: Port,
    out: Port,
  ):
    self.f = f
    self.heap = heap
    self.linker = linker
    self.a = a
    self.b = b
    self.out = out
    self.error = Signal()
    self.ty_id: dict[str, int] = {}
