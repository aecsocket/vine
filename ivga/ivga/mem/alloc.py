from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out

from ivga import ADDR_MAX, Addr, Stat, flatten

from .heap import HeapPort

LANES = 8

class AllocPort(wiring.Signature):
  """
  # Operations

  - `req_alloc(n)`: allocate `n` words on the heap.
    If enough memory was available for all `n` words, `alloc_addrs[n:]` holds
    the addresses of allocated words.
    The order of addresses you get back is unspecified; all other indices in
    `alloc_addrs` are unspecified.
    Otherwise, raises `oom` and does not allocate any memory.

  - `req_free(n)`: free `n` words at addresses `free_addrs[0:n]`.
  """

  def __init__(self):
    super().__init__({
      "req_alloc": Out(range(LANES + 1)),
      "req_free": Out(range(LANES + 1)),
      "free_addrs": Out(ArrayLayout(Addr, LANES)),
      "alloc_addrs": In(ArrayLayout(Addr, LANES)),
      "done": In(1),
      "oom": In(1),
      # stats
      "stat_allocs": In(Stat),
      "stat_frees": In(Stat),
      "stat_peak": In(Addr),
      "stat_reset": Out(1),
    })

  @staticmethod
  def alloc(m: Module, alloc, words):
    m.d.comb += alloc.req_alloc.eq(words)

  @staticmethod
  def free_one(m: Module, alloc, addr):
    m.d.comb += [
      alloc.req_free.eq(1),
      alloc.free_addrs[0].eq(addr),
    ]

  @staticmethod
  def free(m: Module, alloc, words, addrs):
    m.d.comb += [
      alloc.req_free.eq(words),
      alloc.free_addrs.eq(addrs),
    ]


IDLE = "idle"
ALLOC_WAIT = "alloc_wait"
FREE_WAIT = "free_wait"

ADDR_EMPTY = Const(ADDR_MAX, Addr)

class Alloc(wiring.Component):
  control: In(AllocPort())
  heap: Out(HeapPort())

  def __init__(self, capacity: int):
    assert 0 < capacity <= ADDR_MAX

    self.capacity = capacity
    self.empty = Signal(range(capacity + 1))
    self.free_head = Signal(Addr, init=ADDR_EMPTY)
    self.free_count = Signal(Addr)
    self.alloc_top = Signal(range(LANES))
    self.free_addrs_top = Signal(range(LANES))
    self.free_addrs_arr = Signal(ArrayLayout(Addr, LANES))
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

    # stats
    with m.If(self.stat_reset):
      m.d.sync += [
        self.stat_allocs.eq(0),
        self.stat_frees.eq(0),
        self.stat_peak.eq(0),
      ]
    with m.If(self.empty > self.stat_peak):
      m.d.sync += self.stat_peak.eq(self.empty)

    return m

def idle(alloc: Alloc, m: Module):
  available = alloc.capacity - alloc.empty + alloc.free_count

  with m.If(alloc.req_alloc > 0):
    with m.If(alloc.req_alloc > available):
      # tried to allocate too much memory
      m.d.sync += [
        alloc.done.eq(True),
        alloc.oom.eq(True),
      ]
    with m.Elif(alloc.free_head != ADDR_EMPTY):
      # reuse the free head
      # first read the value at the free head
      # e.g. free_head = 10, heap[free_head] = ?
      m.d.sync += alloc.alloc_top.eq(alloc.req_alloc - 1)
      HeapPort.read(m, alloc.heap, alloc.free_head)
      m.next = ALLOC_WAIT
    with m.Else():
      bump_alloc(alloc, m, alloc.req_alloc, 0)

  with m.Elif(alloc.req_free > 0):
    # set `heap[alloc.free_addrs[top]]` to the current free head
    # and set `alloc.free_head` to `alloc.free_addrs[top]`
    # given `top = alloc.req_free - 1`
    #
    # for i in alloc.req_free, we do the same for each `heap[alloc.free_addrs[i]]`
    top = (alloc.req_free - 1).as_unsigned()
    m.d.sync += [
      alloc.free_addrs_top.eq(top),
      alloc.free_addrs_arr.eq(alloc.free_addrs),
    ]
    HeapPort.write(m, alloc.heap, alloc.free_addrs[top], alloc.free_head)
    m.next = FREE_WAIT

def bump_alloc(alloc: Alloc, m: Module, n, extra_allocs: int):
  # wire up only lanes up to `n`,
  # since this is for both the bump-only and mixed alloc paths
  for lane in range(LANES):
    with m.If(lane < n):
      m.d.sync += alloc.alloc_addrs[lane].eq(alloc.empty + lane)

  m.d.sync += [
    alloc.done.eq(True),
    alloc.empty.eq(alloc.empty + n),
    alloc.stat_allocs.eq(alloc.stat_allocs + n + extra_allocs),
  ]
  m.next = IDLE

def alloc_wait(alloc: Alloc, m: Module):
  # wait for heap[free_head] to return
  # keep heap wires stable while waiting
  HeapPort.read(m, alloc.heap, alloc.free_head)
  with m.If(alloc.heap.done):
    m.d.sync += [
      alloc.alloc_addrs[alloc.alloc_top].eq(alloc.free_head),
      alloc.alloc_top.eq(alloc.alloc_top - 1),
      alloc.free_head.eq(alloc.heap.read_data),
      alloc.free_count.eq(alloc.free_count - 1),
      # note: if we go into the bump_alloc branch, we lose this write.
      # that's why we specify the `1` in the call below.
      alloc.stat_allocs.eq(alloc.stat_allocs + 1),
    ]

    with m.If(alloc.alloc_top == 0):
      m.d.sync += alloc.done.eq(True),
      m.next = IDLE
    with m.Elif(alloc.heap.read_data == ADDR_EMPTY):
      # free list is empty but we need more; bump allocate
      # bump alloc'ing is safe since we checked this in idle
      bump_alloc(alloc, m, alloc.alloc_top, 1)

def free_wait(alloc: Alloc, m: Module):
  # wait for memory write to complete
  # keep heap wires stable while waiting
  HeapPort.write(m, alloc.heap, alloc.free_addrs_arr[alloc.free_addrs_top], alloc.free_head)
  with m.If(alloc.heap.done):
    m.d.sync += [
      alloc.free_head.eq(alloc.free_addrs_arr[alloc.free_addrs_top]),
      alloc.free_count.eq(alloc.free_count + 1),
      alloc.free_addrs_top.eq(alloc.free_addrs_top - 1),
      alloc.stat_frees.eq(alloc.stat_frees + 1),
    ]

    with m.If(alloc.free_addrs_top == 0):
      m.d.sync += alloc.done.eq(True)
      m.next = IDLE
    # else keep looping
