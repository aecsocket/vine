from typing import override

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amflow import Operation, ValueLike
from ivga.util.arbiter import make_arbiter_type

from ivga import Addr, Word, flatten

CAPACITY = 1024

class HeapPort(wiring.Signature):
  """
  # Operations

  - `read()`: read the word at `addr`, placing the result in `read_data`.

  - `write()`: write the word `write_data` to address `addr`.

  If both `read()` and `write()` are high on a cycle, then the value at `addr`
  will be swapped with the value in `write_data`, and the old value will be
  available at `read_data`. In Rust terms:

  ```rs
  let read_data = swap(&mut addr, write_data);
  ```
  """

  def __init__(self):
    super().__init__({
      "read": Out(1),
      "write": Out(1),
      "addr": Out(Addr),
      "write_data": Out(Word),
      "granted": In(1),
      "done": In(1),
      "read_data": In(Word),
    })


class Heap(wiring.Component):
  control: In(HeapPort())

  def __init__(self):
    self.memory = Memory(
      shape=Word,
      depth=CAPACITY,
      init=[],
    )
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    memory = m.submodules.memory = self.memory
    read = memory.read_port()
    write = memory.write_port()

    has_request = self.read | self.write
    m.d.comb += [
      read.en.eq(self.read),
      read.addr.eq(self.addr),
      self.read_data.eq(read.data),
      write.en.eq(self.write),
      write.addr.eq(self.addr),
      write.data.eq(self.write_data),
      self.granted.eq(has_request),
    ]
    m.d.sync += self.done.eq(has_request)

    return m


HeapArbiter = make_arbiter_type(
  HeapPort,
  lambda client: client.read | client.write,
)


class HeapRead(Operation[Value]):
  def __init__(self, name: str, heap, addr: ValueLike):
    self.heap = heap
    self.addr = addr

    # when we're in this operation's state, we prefer `heap.read_data`;
    # else, we use the value saved from the last `heap.read_data`
    self.issued = Signal()
    self.just_read = Signal()
    self.saved = Signal(Word)
    result = Signal(Word)
    super().__init__(name, result)

  @override
  def prepare(self, m: Module) -> None:
    m.d.sync += self.just_read.eq(self.issued)
    with m.If(self.just_read):
      m.d.sync += self.saved.eq(self.heap.read_data)

    # `saved` is 1 cycle behind `read.data`;
    # so if we've just read the data, then expose the most recent read data
    latest = Mux(self.just_read, self.heap.read_data, self.saved)
    m.d.comb += self.result.eq(latest)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += [
      self.issued.eq(self.heap.granted),
      self.heap.read.eq(True),
      self.heap.addr.eq(self.addr),
    ]
    return self.heap.granted


class HeapWrite(Operation[None]):
  def __init__(self, name: str, heap, addr: ValueLike, data: ValueLike):
    self.heap = heap
    self.addr = addr
    self.data = data
    super().__init__(name, None)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += [
      self.heap.write.eq(True),
      self.heap.addr.eq(self.addr),
      self.heap.write_data.eq(self.data),
    ]
    return self.heap.granted


class HeapSwap(Operation[Value]):
  def __init__(self, name: str, heap, addr: ValueLike, write_data: ValueLike):
    self.heap = heap
    self.addr = addr
    self.write_data = write_data

    # when we're in this operation's state, we prefer `heap.read_data`;
    # else, we use the value saved from the last `heap.read_data`
    self.issued = Signal()
    self.just_read = Signal()
    self.saved = Signal(Word)
    result = Signal(Word)
    super().__init__(name, result)

  @override
  def prepare(self, m: Module) -> None:
    m.d.sync += self.just_read.eq(self.issued)
    with m.If(self.just_read):
      m.d.sync += self.saved.eq(self.heap.read_data)
    latest = Mux(self.just_read, self.heap.read_data, self.saved)
    m.d.comb += self.result.eq(latest)

  @override
  def emit(self, m: Module) -> Value:
    m.d.comb += [
      self.issued.eq(self.heap.granted),
      self.heap.read.eq(True),
      self.heap.write.eq(True),
      self.heap.addr.eq(self.addr),
      self.heap.write_data.eq(self.write_data),
    ]
    return self.heap.granted
