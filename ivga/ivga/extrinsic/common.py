from collections.abc import Callable, Generator, Iterable
from typing import Any

from amflow import Flow

from ivga import Port
from ivga.mem.heap import HeapPort
from ivga.runtime.linker import LinkerPort

type ExtFnMake = Callable[[ExtFnContext], ExtFnGen]
type ExtFnGen = Generator[object, Any, None] # pyright: ignore[reportExplicitAny]

class ExtTy:
  def __init__(self, name: str, *, is_copy: bool):
    self.name = name
    self.is_copy = is_copy

class ExtFn:
  def __init__(self, name: str, *, inputs: Iterable[str], fn: ExtFnMake):
    self.name = name
    self.inputs = inputs
    self.fn = fn

class ExtFnContext:
  def __init__(
    self,
    f: Flow,
    heap: HeapPort,
    linker: LinkerPort,
    a: Port,
    b: Port,
  ):
    self.f = f
    self.heap = heap
    self.linker = linker
    self.a = a
    self.b = b
    self.ty_id = {}
