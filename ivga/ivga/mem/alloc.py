from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from ivga.mem.heap import HeapPort
from ivga.ty import ADDR_WIDTH, Addr, flatten


class AllocPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_alloc": In(1),
      "req_free": In(1),
      "free_addr": In(Addr),
      "alloc_addr": Out(Addr),
      "done": Out(1),
      "oom": Out(1),
    })


IDLE = "idle"
ALLOC_WAIT = "alloc_wait"
ALLOC_READ = "alloc_read"
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
      with m.State(ALLOC_READ):
        alloc_read(self, m)
      with m.State(FREE_WAIT):
        free_wait(self, m)

    return m

def idle(alloc: Alloc, m: Module):
  with m.If(alloc.req_alloc):
    with m.If(alloc.free_head != ADDR_EMPTY):
      # reuse the free head
      # first read the value at the free head
      # e.g. free_head = 10, heap[free_head] = ?
      m.d.comb += [
        alloc.heap.read_enable.eq(True),
        alloc.heap.read_addr.eq(alloc.free_head),
      ]
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
    m.d.comb += [
      alloc.heap.write_enable.eq(True),
      alloc.heap.write_addr.eq(alloc.free_addr),
      alloc.heap.write_data.eq(alloc.free_head),
    ]
    m.d.sync += alloc.free_head.eq(alloc.free_addr)
    m.next = FREE_WAIT


def alloc_wait(alloc: Alloc, m: Module):
  # wait for heap[free_head] to return
  m.next = ALLOC_READ

def alloc_read(alloc: Alloc, m: Module):
  m.d.sync += [
    alloc.alloc_addr.eq(alloc.free_head),
    alloc.free_head.eq(alloc.heap.read_data),
    alloc.done.eq(True),
  ]
  m.next = IDLE

def free_wait(alloc: Alloc, m: Module):
  # memory write is done; report memory freed
  m.d.sync += alloc.done.eq(True)
  m.next = IDLE
