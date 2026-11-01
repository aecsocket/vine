from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike
from ivga.util.arbiter import make_arbiter_type

from ivga import Addr, Word, flatten

CAPACITY = 1024

class HeapPort(wiring.Signature):
  """
  # Operations

  - `read()`: read the word at `read_addr`, placing the result in `read_data`.

  - `write()`: write the word `write_data` to address `write_addr`.

  If both `read()` and `write()` are high on a cycle, both operations are
  accepted together and may address different words. If the addresses match,
  `read_data` returns the old word, letting you do a read-then-write swap.

  `HeapPort` has no `done` signal; as soon as `granted` is high, on the next
  cycle `read_data` will be available, and `write_data` will have been written.
  This is in contrast to other ports like `AllocPort`, which take a variable
  amount of time to complete after `granted` (that's why `done` exists there).
  """

  def __init__(self):
    super().__init__({
      "read": Out(1),
      "read_addr": Out(Addr),
      "read_data": In(Word),

      "write": Out(1),
      "write_addr": Out(Addr),
      "write_data": Out(Word),

      "granted": In(1),
    })

  @staticmethod
  def read(f: Flow, heap, *, addr: ValueLike) -> ValueLike:
    result = f.capture(heap.read_data, after_step=True)
    f.d.comb += [
      heap.read.eq(True),
      heap.read_addr.eq(addr),
    ]
    f.d.until += heap.granted
    return result

  @staticmethod
  def write(f: Flow, heap, *, addr: ValueLike, write: ValueLike) -> None:
    f.d.comb += [
      heap.write.eq(True),
      heap.write_addr.eq(addr),
      heap.write_data.eq(write),
    ]
    f.d.until += heap.granted

  @staticmethod
  def swap(f: Flow, heap, *, addr: ValueLike, write: ValueLike) -> ValueLike:
    result = f.capture(heap.read_data, after_step=True)
    f.d.comb += [
      heap.read.eq(True),
      heap.write.eq(True),
      heap.read_addr.eq(addr),
      heap.write_addr.eq(addr),
      heap.write_data.eq(write),
    ]
    f.d.until += heap.granted
    return result


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
      read.addr.eq(self.read_addr),
      self.read_data.eq(read.data),
      write.en.eq(self.write),
      write.addr.eq(self.write_addr),
      write.data.eq(self.write_data),
      self.granted.eq(has_request),
    ]

    return m


HeapArbiter = make_arbiter_type(
  HeapPort,
  lambda client: client.read | client.write,
)
