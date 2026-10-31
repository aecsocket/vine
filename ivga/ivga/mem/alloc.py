from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out

from amflow import Flow, Sync, expand, step, sync
from ivga import ADDR_MAX, Addr, Stat, flatten
from ivga.mem.heap import HeapPort, HeapWrite, heap_write

LANES = 8
ADDR_EMPTY = Const(ADDR_MAX, Addr)

class AllocPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_alloc": Out(range(LANES + 1)),
      "req_free": Out(range(LANES + 1)),
      "free_addrs": Out(ArrayLayout(Addr, LANES)),
      "alloc_addrs": In(ArrayLayout(Addr, LANES)),
      "granted": In(1),
      "done": In(1),
      "oom": In(1),
      "bad_request": In(1),
      # stats
      "stat_allocs": In(Stat),
      "stat_frees": In(Stat),
      "stat_peak": In(Addr),
      "stat_reset": Out(1),
    })

class Alloc(wiring.Component):
  control: In(AllocPort())

  def __init__(self, capacity: int, heap: HeapPort):
    assert 0 < capacity <= ADDR_MAX
    assert capacity % 2 == 0

    self.capacity = capacity
    self.heap = heap
    self.empty = Signal(range(capacity + 1))
    self.free_head = Signal(Addr, init=ADDR_EMPTY)
    self.free_count = Signal(range(capacity + 1))
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += [
      self.done.eq(False),
      self.oom.eq(False),
      self.bad_request.eq(False),
    ]

    expand(m, lambda f: program(f, self))

def program(f: Flow, alloc: Alloc):
  available = alloc.capacity - alloc.empty + alloc.free_count

  with f.If(alloc.req_alloc > 0):
    with f.If(alloc.req_alloc > LANES):
      yield sync(f, [
        alloc.done.eq(True),
        alloc.bad_request.eq(True),
      ])
    with f.Elif(alloc.req_alloc > available):
      yield sync(f, [
        alloc.done.eq(True),
        alloc.oom.eq(True),
      ])
    with f.Else():
      do_alloc(f, alloc)

  with f.Elif(alloc.req_free > 0):
    # free `req_free` addresses
    remaining = Signal(range(LANES + 1))
    addrs = Signal(ArrayLayout(Addr, LANES))

    _ = sync(f, [
      remaining.eq(alloc.req_free),
      addrs.eq(alloc.free_addrs),
    ])

    with f.While(remaining != 0):
      addr = addrs[remaining - 1]

      _ = step(f, on_done=[
        alloc.free_head.eq(addr),
        alloc.free_count.eq(alloc.free_count + 1),
        alloc.stat_frees.eq(alloc.stat_frees + 1),
        remaining.eq(remaining - 1),
      ])
      yield heap_write(f, alloc.heap, addr=addr, data=alloc.free_head)

    yield sync(alloc.done.eq(True))

def do_alloc(f: Flow, alloc: Alloc):
  # general idea: allocate from the free list if available,
  # else bump allocate.
  with f.If(alloc.free_head == ADDR_EMPTY):
    # bump allocate
    pass
