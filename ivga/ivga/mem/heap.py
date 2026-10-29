from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
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

  @staticmethod
  def read(m: Module, heap, addr):
    m.d.comb += [
      heap.read.eq(True),
      heap.addr.eq(addr),
    ]

  @staticmethod
  def write(m: Module, heap, addr, data):
    m.d.comb += [
      heap.write.eq(True),
      heap.addr.eq(addr),
      heap.write_data.eq(data),
    ]

  @staticmethod
  def swap(m: Module, heap, addr, data):
    m.d.comb += [
      heap.read.eq(True),
      heap.write.eq(True),
      heap.addr.eq(addr),
      heap.write_data.eq(data),
    ]


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
