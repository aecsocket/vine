from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
from ivga.util.arbiter import make_arbiter_type

from ivga import ADDR_MAX, Addr, Stat, flatten

from .heap import HeapPort

LANES = 8

class AllocPort(wiring.Signature):
  """
  # Operations

  - `req_alloc(n)`: allocate an even number of words, up to `LANES`.
    `alloc_addrs[:n]` contains aligned pairs: lanes `(2*i, 2*i+1)` hold
    `(base, base+1)`, with even `base`. Pair ordering is unspecified.
    Odd counts, counts above `LANES`, and insufficient complete pairs raise
    `done` and `oom` without allocating anything. Zero means no request.

  - `req_free(n)`: free the individual words at `free_addrs[:n]`, in reverse
    order. A pair becomes reusable only after both halves have been freed.
    Counts must be in `1..LANES`; invalid/double frees are caller errors.

  Hold requests and their payloads until a clock edge with `granted`, then
  withdraw them. `done` acknowledges completion, including all heap responses.
  Allocation has priority over a simultaneous free request. Allocated heap
  contents are unspecified and must be initialized by the caller.

  Statistics count words. `stat_peak` is the bump-pointer high-water mark,
  not live usage. Resetting statistics does not reset allocator ownership.
  """

  def __init__(self):
    super().__init__({
      "req_alloc": Out(range(LANES + 1)),
      "req_free": Out(range(LANES + 1)),
      "free_addrs": Out(ArrayLayout(Addr, LANES)),
      "alloc_addrs": In(ArrayLayout(Addr, LANES)),
      "granted": In(1),
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
ALLOC_REQ = "alloc_req"
ALLOC_WAIT = "alloc_wait"
FREE_REQ = "free_req"
FREE_WAIT = "free_wait"

ADDR_EMPTY = Const(ADDR_MAX, Addr)

class Alloc(wiring.Component):
  control: In(AllocPort())
  heap: Out(HeapPort())

  def __init__(self, capacity: int):
    assert 0 < capacity <= ADDR_MAX and capacity % 2 == 0, \
      "allocator capacity must be an even number of words within the address space"

    self.capacity = capacity
    self.empty = Signal(range(capacity + 1))
    self.free_head = Signal(Addr, init=ADDR_EMPTY)
    # Only fully reclaimed pairs contribute to free_count, in words.
    self.free_count = Signal(Addr)
    # One bit per pair: exactly one of its halves has been freed. Which half
    # does not matter because invalid/double frees are caller errors.
    self.half_free = Signal(capacity // 2)
    self.alloc_top = Signal(range(LANES // 2))
    self.free_addrs_top = Signal(range(LANES))
    self.free_addrs_arr = Signal(ArrayLayout(Addr, LANES))
    self.free_pending = Signal(range(LANES // 2 + 1))
    self.free_write = Signal()
    self.free_response = Signal()
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()

    m.d.sync += [
      self.done.eq(False),
      self.oom.eq(False),
    ]

    with m.FSM(init=IDLE) as fsm:
      with m.State(IDLE):
        idle(m, self)
      with m.State(ALLOC_REQ):
        alloc_req(m, self, self.free_head)
      with m.State(ALLOC_WAIT):
        alloc_wait(m, self)
      with m.State(FREE_REQ):
        free_req(m, self, self.free_addrs_arr[self.free_addrs_top], self.free_addrs_top)
      with m.State(FREE_WAIT):
        free_wait(m, self)

    # Metadata writes can overlap processing subsequent frees. Do not accept a
    # new allocator command until every accepted write has been acknowledged.
    m.d.comb += self.free_response.eq(
      (fsm.ongoing(FREE_REQ) | fsm.ongoing(FREE_WAIT)) & self.heap.done
    )
    m.d.sync += self.free_pending.eq(
      self.free_pending + self.free_write - self.free_response
    )

    with m.If(self.stat_reset):
      m.d.sync += [
        self.stat_allocs.eq(0),
        self.stat_frees.eq(0),
        self.stat_peak.eq(0),
      ]
    with m.Elif(self.empty > self.stat_peak):
      m.d.sync += self.stat_peak.eq(self.empty)

    return m


def idle(m: Module, alloc: Alloc):
  available = alloc.capacity - alloc.empty + alloc.free_count
  m.d.comb += alloc.granted.eq(True)

  with m.If(alloc.req_alloc > 0):
    with m.If(alloc.req_alloc[0] | (alloc.req_alloc > LANES) | (alloc.req_alloc > available)):
      m.d.sync += [
        alloc.done.eq(True),
        alloc.oom.eq(True),
      ]
    with m.Elif(alloc.free_head != ADDR_EMPTY):
      m.d.sync += alloc.alloc_top.eq((alloc.req_alloc >> 1) - 1)
      alloc_req(m, alloc, alloc.free_head)
    with m.Else():
      bump_alloc(m, alloc, alloc.req_alloc, 0)

  with m.Elif(alloc.req_free > 0):
    top = (alloc.req_free - 1).as_unsigned()
    m.d.sync += [
      alloc.free_addrs_top.eq(top),
      alloc.free_addrs_arr.eq(alloc.free_addrs),
    ]
    free_req(m, alloc, alloc.free_addrs[top], top)


def bump_alloc(m: Module, alloc: Alloc, n, extra_allocs: int):
  # Recycled pairs occupy higher lanes; leave those results intact.
  for lane in range(LANES):
    with m.If(lane < n):
      m.d.sync += alloc.alloc_addrs[lane].eq(alloc.empty + lane)

  m.d.sync += [
    alloc.done.eq(True),
    alloc.empty.eq(alloc.empty + n),
    alloc.stat_allocs.eq(alloc.stat_allocs + n + extra_allocs),
  ]
  m.next = IDLE


def alloc_req(m: Module, alloc: Alloc, free_head):
  HeapPort.read(m, alloc.heap, free_head)
  with m.If(alloc.heap.granted):
    m.next = ALLOC_WAIT
  with m.Else():
    m.next = ALLOC_REQ


def alloc_wait(m: Module, alloc: Alloc):
  with m.If(alloc.heap.done):
    next_free_head = alloc.heap.read_data
    lane = alloc.alloc_top << 1
    m.d.sync += [
      alloc.alloc_addrs[lane].eq(alloc.free_head),
      alloc.alloc_addrs[lane + 1].eq(alloc.free_head + 1),
      alloc.alloc_top.eq(alloc.alloc_top - 1),
      alloc.free_head.eq(next_free_head),
      alloc.free_count.eq(alloc.free_count - 2),
      alloc.stat_allocs.eq(alloc.stat_allocs + 2),
    ]

    with m.If(alloc.alloc_top == 0):
      m.d.sync += alloc.done.eq(True)
      m.next = IDLE
    with m.Elif(next_free_head == ADDR_EMPTY):
      bump_alloc(m, alloc, alloc.alloc_top << 1, 2)
    with m.Else():
      alloc_req(m, alloc, next_free_head)


def free_req(m: Module, alloc: Alloc, addr, top):
  half_free = Value.cast(alloc.half_free.bit_select(addr[1:], 1))
  base = Cat(Const(0, 1), addr[1:])

  with m.If(half_free):
    HeapPort.write(m, alloc.heap, base, alloc.free_head)

  with m.If(~half_free | alloc.heap.granted):
    m.d.sync += [
      half_free.eq(~half_free),
      alloc.stat_frees.eq(alloc.stat_frees + 1),
    ]
    with m.If(half_free):
      m.d.comb += alloc.free_write.eq(True)
      m.d.sync += [
        alloc.free_head.eq(base),
        alloc.free_count.eq(alloc.free_count + 2),
      ]

    with m.If(top == 0):
      with m.If(alloc.free_pending + alloc.free_write == alloc.free_response):
        m.d.sync += alloc.done.eq(True)
        m.next = IDLE
      with m.Else():
        m.next = FREE_WAIT
    with m.Else():
      m.d.sync += alloc.free_addrs_top.eq(top - 1)
      m.next = FREE_REQ
  with m.Else():
    m.next = FREE_REQ


def free_wait(m: Module, alloc: Alloc):
  with m.If(alloc.free_response & (alloc.free_pending == 1)):
    m.d.sync += alloc.done.eq(True)
    m.next = IDLE


AllocArbiter = make_arbiter_type(
  AllocPort,
  lambda client: (client.req_alloc != 0) | (client.req_free != 0),
  exclude=("stat_allocs", "stat_frees", "stat_peak", "stat_reset"),
)
