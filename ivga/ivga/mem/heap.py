from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amflow import Capture, Flow, ValueLike
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

  `HeapPort` has no `done` signal. At the edge accepting a request (`granted`
  high), writes commit and reads enter the BRAM. Read data becomes available
  after the following edge, through the BRAM output register. Requests can still
  be accepted on consecutive edges; only the read latency is increased.
  This is in contrast to other ports like `AllocPort`, which take a variable
  amount of time to complete after `granted` (that's why `done` exists there).

  `Heap` operates on raw `Word`s, not structured `Port`s, since many subsystems
  may use the heap for their own allocations, not just allocating port pairs.
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
  def read(f: Flow, heap, *, addr: ValueLike) -> Capture:
    accepted = Signal()
    result = f.capture(heap.read_data, after_step=True)
    f.d.comb += [
      heap.read.eq(~accepted),
      heap.read_addr.eq(addr),
    ]
    f.d.sync += accepted.eq(accepted | heap.granted)
    f.d.until += accepted
    f.d.on_done += accepted.eq(False)
    return result

  @staticmethod
  def write(f: Flow, heap, *, addr: ValueLike, write: ValueLike) -> None:
    accepted = Signal()
    f.d.comb += [
      heap.write.eq(~accepted),
      heap.write_addr.eq(addr),
      heap.write_data.eq(write),
    ]
    f.d.sync += accepted.eq(accepted | heap.granted)
    f.d.until += accepted | heap.granted
    f.d.on_done += accepted.eq(False)

  @staticmethod
  def swap(f: Flow, heap, *, addr: ValueLike, write: ValueLike) -> Capture:
    accepted = Signal()
    result = f.capture(heap.read_data, after_step=True)
    f.d.comb += [
      heap.read.eq(~accepted),
      heap.write.eq(~accepted),
      heap.read_addr.eq(addr),
      heap.write_addr.eq(addr),
      heap.write_data.eq(write),
    ]
    f.d.sync += accepted.eq(accepted | heap.granted)
    f.d.until += accepted
    f.d.on_done += accepted.eq(False)
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

    # Keep the extra data stage resetless and immediately after the memory so
    # synthesis can absorb it into the BRAM's built-in output register.
    read_pending = Signal()
    read_data = Signal(Word, reset_less=True)
    m.d.sync += read_pending.eq(self.read)
    with m.If(read_pending):
      m.d.sync += read_data.eq(read.data)

    has_request = self.read | self.write
    m.d.comb += [
      read.en.eq(self.read),
      read.addr.eq(self.read_addr),
      self.read_data.eq(read_data),
      write.en.eq(self.write),
      write.addr.eq(self.write_addr),
      write.data.eq(self.write_data),
      self.granted.eq(has_request),
    ]

    return m


HeapArbiter = make_arbiter_type(
  HeapPort,
  lambda client: client.read | client.write,
  response_latency=2,
  register_grant=True,
)
