from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amflow import Flow, Step, ValueLike, step
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

  `HeapPort` has no `done` signal; as soon as `granted` is high, on the next
  cycle `read_data` will be available, and `write_data` will have been written.
  This is in contrast to other ports like `AllocPort`, which take a variable
  amount of time to complete after `granted` (that's why `done` exists there).
  """

  def __init__(self):
    super().__init__({
      "read": Out(1),
      "write": Out(1),
      "addr": Out(Addr),
      "write_data": Out(Word),
      "granted": In(1),
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

    return m


HeapArbiter = make_arbiter_type(
  HeapPort,
  lambda client: client.read | client.write,
)

def heap_read(f: Flow, heap, *, addr: ValueLike) -> Step:
  issued = Signal()
  just_read = Signal()
  saved = Signal.like(heap.read_data)

  m = f.m
  m.d.sync += just_read.eq(issued)
  with m.If(just_read):
    m.d.sync += saved.eq(heap.read_data)
  latest = Mux(just_read, heap.read_data, saved)

  return step(
    f,
    comb=[
      heap.read.eq(True),
      heap.addr.eq(addr),
      issued.eq(heap.granted),
    ],
    until=heap.granted,
    out=latest,
  )

def heap_write(f: Flow, heap, *, addr: ValueLike, data: ValueLike) -> Step:
  return step(
    f,
    comb=[
      heap.write.eq(True),
      heap.addr.eq(addr),
      heap.write_data.eq(data),
    ],
    until=heap.granted,
  )

def heap_swap(f: Flow, heap, *, addr: ValueLike, write_data: ValueLike) -> Step:
  issued = Signal()
  just_read = Signal()
  saved = Signal.like(heap.read_data)

  m = f.m
  m.d.sync += just_read.eq(issued)
  with m.If(just_read):
    m.d.sync += saved.eq(heap.read_data)
  latest = Mux(just_read, heap.read_data, saved)

  return step(
    f,
    comb=[
      heap.read.eq(True),
      heap.write.eq(True),
      heap.addr.eq(addr),
      heap.write_data.eq(write_data),
      issued.eq(heap.granted),
    ],
    until=heap.granted,
    out=latest,
  )
