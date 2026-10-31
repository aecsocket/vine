from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike, expand
from ivga.mem.heap import HeapPort
from ivga.util.arbiter import make_arbiter_type

from ivga import ADDR_MAX, Addr, Stat, flatten

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
      # stats
      "stat_allocs": In(Stat),
      "stat_frees": In(Stat),
      "stat_peak": In(Addr),
      "stat_reset": Out(1),
    })

  @staticmethod
  def alloc(f: Flow, alloc, *, n_words: ValueLike) -> tuple[Signal, Signal]:
    accepted = Signal()
    addrs, oom = Signal.like(alloc.alloc_addrs), Signal.like(alloc.oom)

    f.d.comb += alloc.req_alloc.eq(Mux(accepted, False, n_words))
    f.d.sync += [
      accepted.eq(accepted | alloc.granted),
      Assert(accepted | (Value.cast(n_words) >= 1), "Allocation requires at least one word"),
    ]
    f.d.until += accepted & alloc.done
    f.d.on_done += [
      accepted.eq(False),
      addrs.eq(alloc.alloc_addrs),
      oom.eq(alloc.oom),
    ]
    return addrs, oom

  @staticmethod
  def free(f: Flow, alloc, *, n_words: ValueLike, addrs: ValueLike) -> None:
    accepted = Signal()

    f.d.comb += [
      alloc.req_free.eq(Mux(accepted, False, n_words)),
      alloc.free_addrs.eq(addrs),
    ]
    f.d.sync += [
      accepted.eq(accepted | alloc.granted),
      Assert(accepted | (Value.cast(n_words) >= 1), "Free requires at least one word"),
    ]
    f.d.until += accepted & alloc.done
    f.d.on_done += accepted.eq(False)

  @staticmethod
  def free_one(f: Flow, alloc, *, addr: ValueLike) -> None:
    AllocPort.free(f, alloc, n_words=1, addrs=addr)


class Alloc(wiring.Component):
  control: In(AllocPort())
  heap: Out(HeapPort())

  def __init__(self, capacity: int):
    assert 0 < capacity <= ADDR_MAX
    assert capacity % 2 == 0

    self.capacity = capacity
    self.empty = Signal(range(capacity + 1))
    self.free_head = Signal(Addr, init=ADDR_EMPTY)
    self.free_count = Signal(range(capacity + 1))
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m, alloc = Module(), self
    m.d.sync += [
      alloc.done.eq(False),
      alloc.oom.eq(False),
    ]
    expand(m, lambda f: program(f, alloc))

    with m.If(alloc.empty > alloc.stat_peak):
      m.d.sync += alloc.stat_peak.eq(alloc.empty)
    with m.If(alloc.stat_reset):
      m.d.sync += [
        alloc.stat_allocs.eq(0),
        alloc.stat_frees.eq(0),
        alloc.stat_peak.eq(0),
      ]

    return m

def program(f: Flow, alloc: Alloc):
  f.d.comb += alloc.granted.eq(True)
  available = alloc.capacity - alloc.empty + alloc.free_count

  with f.If(alloc.req_alloc > 0):
    # allocate `req_alloc` words of memory
    f.d.sync += Assert(alloc.req_alloc <= LANES, "Allocation request exceeds lane count")

    with f.If(alloc.req_alloc > available):
      f.d.sync += [
        alloc.done.eq(True),
        alloc.oom.eq(True),
      ]
      yield
    with f.Else():
      yield from do_alloc(f, alloc)

  with f.Elif(alloc.req_free > 0):
    # free `req_free` words of memory
    f.d.sync += Assert(alloc.req_free <= LANES, "Free request exceeds lane count")

    remaining = f.capture(alloc.req_free)
    addrs = f.capture(alloc.free_addrs)

    with f.While(remaining != 0):
      addr = addrs[(remaining - 1).as_unsigned()]

      f.d.on_done += [
        alloc.free_head.eq(addr),
        alloc.free_count.eq(alloc.free_count + 1),
        alloc.stat_frees.eq(alloc.stat_frees + 1),
        remaining.eq(remaining - 1),
      ]
      yield HeapPort.write(f, alloc.heap, addr=addr, write=alloc.free_head)

    f.d.sync += alloc.done.eq(True)
    yield

def do_alloc(f: Flow, alloc: Alloc):
  # general idea: allocate from the free list if available,
  # else bump allocate.
  req_alloc = f.capture(alloc.req_alloc)

  with f.While((req_alloc != 0) & (alloc.free_head != ADDR_EMPTY)):
    # prioritize taking from the free list
    top_idx = (req_alloc - 1).as_unsigned()
    f.d.sync += alloc.alloc_addrs[top_idx].eq(alloc.free_head)
    next_free_head = yield HeapPort.read(f, alloc.heap, addr=alloc.free_head)

    f.d.on_done += [
      req_alloc.eq(req_alloc - 1),
      alloc.free_count.eq(alloc.free_count - 1),
      alloc.stat_allocs.eq(alloc.stat_allocs + 1),
    ]
    f.d.sync += alloc.free_head.eq(next_free_head)
    yield

  with f.If(req_alloc != 0):
    # consumed the whole free list but we still haven't allocated enough;
    # take from the bump allocator.
    # we've already asserted that we have enough free capacity.
    for lane in range(LANES):
      lane_alloc = alloc.alloc_addrs[lane]
      f.d.sync += lane_alloc.eq(
        Mux(lane < req_alloc, alloc.empty + lane, lane_alloc)
      )
    yield

  f.d.sync += [
    alloc.done.eq(True),
    alloc.empty.eq(alloc.empty + req_alloc),
    alloc.stat_allocs.eq(alloc.stat_allocs + req_alloc),
  ]
  yield


AllocArbiter = make_arbiter_type(
  AllocPort,
  lambda client: (client.req_alloc != 0) | (client.req_free != 0),
)
