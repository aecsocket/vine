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
  def __init__(self, name: str, port: ReadPort, addr: ValueLike):
    super().__init__(name, Value.cast(port.data))
    self.read = port
    self.addr = Value.cast(addr)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += [
      self.read.en.eq(True),
      self.read.addr.eq(self.addr),
    ]
    return Const(True)


class WriteMemory(Operation[None]):
  def __init__(self, name: str, port: WritePort, addr: ValueLike, data: ValueLike):
    super().__init__(name, None)
    self.write = port
    self.addr = Value.cast(addr)
    self.data = Value.cast(data)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += [
      self.write.en.eq(True),
      self.write.addr.eq(self.addr),
      self.write.data.eq(self.data),
    ]
    return Const(True)
