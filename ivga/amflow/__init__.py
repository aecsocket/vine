from abc import ABC, abstractmethod
from collections.abc import Generator
from enum import Enum
from typing import Any, override

from amaranth import *
from amaranth import ValueCastable
from amaranth.lib.memory import ReadPort, WritePort

type ValueLike = Value | ValueCastable | Enum | int

class Operation[T](ABC):
  def __init__(self, name: str, result: T):
    self._name = name
    self._result = result

  @property
  def name(self) -> str:
    """Name of this stage, like `write_free_addr` or `bump_value`."""
    return self._name

  @property
  def result(self) -> T:
    """Response signal from this operation."""
    return self._result

  def prepare(self, m: Module) -> None:
    """Emit elaboration instructions for this operation, which are not gated by
    the operation currently running.
    """

  @abstractmethod
  def emit(self, m: Module) -> Value:
    """Emit elaboration instructions for when this operation is running.

    Returns:
      Value expressing whether this operation has completed by the next clock
      edge.
    """
    ...

def elaborate_flow(
  m: Module,
  gen: Generator[
    Operation[object],
    Any, # pyright: ignore[reportExplicitAny]
    None,
  ],
) -> None:
  operations = []
  result = None

  while True:
    try:
      operation = gen.send(result)
    except StopIteration:
      break
    operations.append(operation)
    result = operation.result

  elaborate_flow_operations(m, operations)

def elaborate_flow_operations(
  m: Module,
  operations: list[Operation[object]],
) -> None:
  if not operations:
    # nothing to elaborate
    return
  first = operations[0]
  first_state_name = f"0_{first.name}"

  for operation in operations:
    operation.prepare(m)

  with m.FSM(init=first_state_name):
    for i, operation in enumerate(operations):
      next_operation = operations[i + 1] if i + 1 < len(operations) else None
      state_name = f"{i}_{operation.name}"
      next_state_name = f"{i+1}_{next_operation.name}" if next_operation else first_state_name

      with m.State(state_name):
        cond = operation.emit(m)
        with m.If(cond):
          m.next = next_state_name


class ReadMemory(Operation[Value]):
  def __init__(self, name: str, read: ReadPort, addr: ValueLike):
    self.read = read
    self.addr = Value.cast(addr)

    # when we're in this operation's state, we prefer `read.data`;
    # else, we use the value saved from the last `read.data`
    self.issued = Signal()
    self.just_read = Signal()
    self.saved = Signal(read.signature.shape)
    result = Signal(read.signature.shape)
    super().__init__(name, result)

  @override
  def prepare(self, m: Module) -> None:
    m.d.sync += self.just_read.eq(self.issued)
    with m.If(self.just_read):
      m.d.sync += self.saved.eq(self.read.data)

    # `saved` is 1 cycle behind `read.data`;
    # so if we've just read the data, then expose the most recent read data
    latest = Mux(self.just_read, self.read.data, self.saved)
    m.d.comb += self.result.eq(latest)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += [
      self.issued.eq(True),
      self.read.en.eq(True),
      self.read.addr.eq(self.addr),
    ]
    return Const(True)


class WriteMemory(Operation[None]):
  def __init__(self, name: str, write: WritePort, addr: ValueLike, data: ValueLike):
    self.write = write
    self.addr = Value.cast(addr)
    self.data = Value.cast(data)
    super().__init__(name, None)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += [
      self.write.en.eq(True),
      self.write.addr.eq(self.addr),
      self.write.data.eq(self.data),
    ]
    return Const(True)


class Step(Operation[None]):
  def __init__(self, name: str, *, comb, sync, until):
    self.comb = comb
    self.sync = sync
    self.until = until
    super().__init__(name, None)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += self.comb
    with m.If(self.until):
      m.d.sync += self.sync
    return self.until


class Comb(Operation[None]):
  def __init__(self, name: str, assigns):
    self.assigns = assigns if isinstance(assigns, list) else [assigns]
    super().__init__(name, None)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += self.assigns
    return Const(True)


class Sync(Operation[None]):
  def __init__(self, name: str, assigns):
    self.assigns = assigns if isinstance(assigns, list) else [assigns]
    super().__init__(name, None)

  @override
  def emit(self, m: Module) -> Value:
    m.d.sync += self.assigns
    return Const(True)
