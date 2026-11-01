from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike, expand
from ivga.mem.heap import HeapPort
from ivga.util.arbiter import make_arbiter_type

from ivga import ADDR_MAX, Addr, Port, Stat, flatten

ALLOC_LANES = 4
"""How many pairs (2 words) can be allocated at once."""

FREE_LANES = 2
"""How many single words can be freed at once."""

ADDR_EMPTY = Const(ADDR_MAX, Addr)

class AllocPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_alloc": Out(range(ALLOC_LANES + 1)),
      "alloc_bases": In(ArrayLayout(Addr, ALLOC_LANES)),

      "req_free": Out(range(FREE_LANES + 1)),
      "free_addrs": Out(ArrayLayout(Addr, FREE_LANES)),

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
  def alloc(f: Flow, alloc, *, n_pairs: ValueLike) -> tuple[Signal, Signal]:
    accepted = Signal()
    bases, oom = Signal.like(alloc.alloc_bases), Signal.like(alloc.oom)

    f.d.comb += alloc.req_alloc.eq(Mux(accepted, 0, n_pairs))
    f.d.sync += [
      accepted.eq(accepted | alloc.granted),
      Assert(accepted | (Value.cast(n_pairs) >= 1), "Allocation requires at least one pair"),
    ]
    f.d.until += accepted & alloc.done
    f.d.on_done += [
      accepted.eq(False),
      bases.eq(alloc.alloc_bases),
      oom.eq(alloc.oom),
    ]
    return bases, oom

  @staticmethod
  def free_all(f: Flow, alloc, *, n_words: ValueLike, addrs: ValueLike) -> None:
    accepted = Signal()

    f.d.comb += [
      alloc.req_free.eq(Mux(accepted, 0, n_words)),
      alloc.free_addrs.eq(addrs),
    ]
    f.d.sync += [
      accepted.eq(accepted | alloc.granted),
      Assert(accepted | (Value.cast(n_words) >= 1), "Free requires at least one word"),
    ]
    f.d.until += accepted & alloc.done
    f.d.on_done += accepted.eq(False)

  @staticmethod
  def free(f: Flow, alloc, *, addr: ValueLike) -> None:
    AllocPort.free_all(f, alloc, n_words=1, addrs=addr)


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
    # allocate `req_alloc` word-pairs of memory
    f.d.sync += Assert(alloc.req_alloc <= ALLOC_LANES, "Allocation request exceeds lane count")

    with f.If((alloc.req_alloc * 2) > available):
      f.d.sync += [
        alloc.done.eq(True),
        alloc.oom.eq(True),
      ]
      yield
    with f.Else():
      yield from do_alloc(f, alloc)

  with f.Elif(alloc.req_free > 0):
    # free `req_free` words of memory
    f.d.sync += Assert(alloc.req_free <= FREE_LANES, "Free request exceeds lane count")
    yield from free(f, alloc)

def do_alloc(f: Flow, alloc: Alloc):
  # general idea: allocate from the free list if available,
  # else bump allocate.
  pairs_to_alloc = f.capture(alloc.req_alloc)

  with f.While((pairs_to_alloc != 0) & (alloc.free_head != ADDR_EMPTY)):
    # prioritize taking from the free list
    # one free list entry represents *two* allocations
    # (since we always allocate in multiples of 2):
    # - `free_head` is the 1st allocated word
    # - `free_head ^ 1` is the 2nd allocated word
    #   the caller is responsible for computing this
    top_idx = (pairs_to_alloc - 1).as_unsigned()
    f.d.sync += alloc.alloc_bases[top_idx].eq(alloc.free_head)
    next_free_head = yield HeapPort.read(f, alloc.heap, addr=alloc.free_head)

    f.d.on_done += [
      pairs_to_alloc.eq(pairs_to_alloc - 1),
      alloc.free_count.eq(alloc.free_count - 2),
      alloc.stat_allocs.eq(alloc.stat_allocs + 2),
    ]
    f.d.sync += alloc.free_head.eq(Port(next_free_head).payload)
    yield

  with f.If(pairs_to_alloc != 0):
    # consumed the whole free list but we still haven't allocated enough;
    # take from the bump allocator.
    # we've already asserted that we have enough free capacity.
    for lane in range(ALLOC_LANES):
      lane_base = alloc.alloc_bases[lane]
      f.d.sync += lane_base.eq(
        Mux(lane < pairs_to_alloc, alloc.empty + lane * 2, lane_base)
      )
    f.d.sync += [
      alloc.empty.eq(alloc.empty + pairs_to_alloc * 2),
      alloc.stat_allocs.eq(alloc.stat_allocs + pairs_to_alloc * 2),
    ]

  f.d.sync += alloc.done.eq(True)
  yield

def free(f: Flow, alloc: Alloc):
  remaining = f.capture(alloc.req_free)
  addrs = f.capture(alloc.free_addrs)
  freed = Port.freed().as_value()

  with f.While(remaining != 0):
    addr = addrs[(remaining - 1).as_unsigned()]
    left = Addr.left_half(addr)

    # store a marker saying this word is free now,
    # and read the other half of this pair
    # to see if we've already freed the other half
    other = HeapPort.read(f, alloc.heap, addr=Addr.other_half(addr))
    yield HeapPort.write(f, alloc.heap, addr=addr, write=freed)

    f.d.on_done += [
      alloc.stat_frees.eq(alloc.stat_frees + 1),
      remaining.eq(remaining - 1),
    ]

    with f.If(other == freed):
      # we previously freed the other half of this pair,
      # and we just freed this half, so the pair is now free;
      # mark it as such.
      # if we haven't freed the other half yet,
      # then we don't add it to the free list,
      # cause this pair isn't safe to reuse yet.
      f.d.on_done += [
        # add the *left* half to the free list,
        # not the address we just freed,
        # since we might have freed the non-left word,
        # breaking the free list precondition.
        alloc.free_head.eq(left),
        alloc.free_count.eq(alloc.free_count + 2),
      ]
      HeapPort.write(f, alloc.heap, addr=left, write=Port.with_addr(alloc.free_head))

    yield

  f.d.sync += alloc.done.eq(True)
  yield

AllocArbiter = make_arbiter_type(
  AllocPort,
  lambda client: (client.req_alloc != 0) | (client.req_free != 0),
)
