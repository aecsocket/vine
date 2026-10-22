from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from ivga.mem.heap import HeapPort
from ivga.ty import ADDR_WIDTH, Addr, flatten


class AllocPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_alloc": Out(1),
      "req_free": Out(1),
      "free_addr": Out(Addr),
      "alloc_addr": In(Addr),
      "done": In(1),
      "oom": In(1),
    })

  @staticmethod
  def alloc(m: Module, alloc):
    m.d.comb += alloc.req_alloc.eq(True)

  @staticmethod
  def free(m: Module, alloc, addr):
    m.d.comb += [
      alloc.req_free.eq(True),
      alloc.free_addr.eq(addr),
    ]


IDLE = "idle"
ALLOC_WAIT = "alloc_wait"
FREE_WAIT = "free_wait"

ADDR_EMPTY = Const((1 << ADDR_WIDTH) - 1, Addr)

class Alloc(wiring.Component):
  control: In(AllocPort())
  heap: Out(HeapPort())

  def __init__(self, capacity: int):
    assert 0 < capacity <= (1 << ADDR_WIDTH) - 1

    self.capacity = capacity
    self.empty = Signal(range(capacity + 1))
    self.free_head = Signal(Addr, init=ADDR_EMPTY)
    self.free_addr_pending = Signal(Addr)
    super().__init__()

    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()

    m.d.sync += [
      self.done.eq(False),
      self.oom.eq(False),
    ]

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(self, m)
      with m.State(ALLOC_WAIT):
        alloc_wait(self, m)
      with m.State(FREE_WAIT):
        free_wait(self, m)

    return m

def idle(alloc: Alloc, m: Module):
  with m.If(alloc.req_alloc):
    with m.If(alloc.free_head != ADDR_EMPTY):
      # reuse the free head
      # first read the value at the free head
      # e.g. free_head = 10, heap[free_head] = ?
      HeapPort.read(m, alloc.heap, alloc.free_head)
      m.next = ALLOC_WAIT

    with m.Elif(alloc.empty < alloc.capacity):
      # bump allocate; we still have empty memory
      m.d.sync += [
        alloc.alloc_addr.eq(alloc.empty),
        alloc.empty.eq(alloc.empty + 1),
        alloc.done.eq(True),
      ]

    with m.Else():
      # tried to bump allocate, but out of memory
      m.d.sync += [
        alloc.done.eq(True),
        alloc.oom.eq(True),
      ]

  with m.Elif(alloc.req_free):
    # set `heap[alloc.free_addr]` to the current free head
    # and set `alloc.free_head` to `alloc.free_addr`
    HeapPort.write(m, alloc.heap, alloc.free_addr, alloc.free_head)
    m.d.sync += alloc.free_addr_pending.eq(alloc.free_addr)
    m.next = FREE_WAIT


def alloc_wait(alloc: Alloc, m: Module):
  # wait for heap[free_head] to return
  # keep heap wires stable while waiting
  HeapPort.read(m, alloc.heap, alloc.free_head)
  with m.If(alloc.heap.done):
    m.d.sync += [
      alloc.alloc_addr.eq(alloc.free_head),
      alloc.free_head.eq(alloc.heap.read_data),
      alloc.done.eq(True),
    ]
    m.next = IDLE

def free_wait(alloc: Alloc, m: Module):
  # wait for memory write to complete
  # keep heap wires stable while waiting
  HeapPort.write(m, alloc.heap, alloc.free_addr_pending, alloc.free_head)
  with m.If(alloc.heap.done):
    m.d.sync += [
      alloc.free_head.eq(alloc.free_addr_pending),
      alloc.done.eq(True)
    ]
    m.next = IDLE
