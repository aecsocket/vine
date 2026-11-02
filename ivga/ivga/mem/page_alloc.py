from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike, expand
from ivga.mem.heap import HeapPort
from ivga.util.arbiter import make_arbiter_type

from ivga import ADDR_MAX, Addr, Stat, flatten

PAGE_SHIFT = 7
"""`2 ** PAGE_SHIFT` is the number of words that fit in a page."""

PAGE_SIZE = 1 << PAGE_SHIFT
"""Number of words that fit in a page."""

PAGE_ID_WIDTH = Addr.width - PAGE_SHIFT

PageId = unsigned(PAGE_ID_WIDTH)
"""Index of a memory page.

Use `page_id << PAGE_SHIFT` to get the base memory address of a page,
and `mem_addr >> PAGE_SHIFT` to get the page ID of a memory address."""

LANES = 1
"""How many pages can be allocated or freed at once."""

class PageAllocPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_alloc": Out(range(LANES + 1)),
      "alloc_ids": In(ArrayLayout(PageId, LANES)),

      "req_free": Out(range(LANES + 1)),
      "free_ids": Out(ArrayLayout(PageId, LANES)),

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
  def alloc(f: Flow, pages, *, n_pages: ValueLike) -> tuple[Signal, Signal]:
    accepted = Signal()
    page_ids, oom = Signal.like(pages.alloc_ids), Signal.like(pages.oom)

    f.d.comb += pages.req_alloc.eq(Mux(accepted, 0, n_pages))
    f.d.sync += accepted.eq(accepted | pages.granted)
    f.d.until += accepted & pages.done
    f.d.on_done += [
      accepted.eq(False),
      page_ids.eq(pages.alloc_ids),
      oom.eq(pages.oom)
    ]
    return page_ids, oom

  @staticmethod
  def free(f: Flow, pages, *, n_pages: ValueLike, page_ids: ValueLike):
    accepted = Signal()
    f.d.comb += [
      pages.req_free.eq(Mux(accepted, 0, n_pages)),
      pages.free_ids.eq(page_ids),
    ]
    f.d.sync += accepted.eq(accepted | pages.granted)
    f.d.until += accepted & pages.done
    f.d.on_done += accepted.eq(False)


class PageAlloc(wiring.Component):
  control: In(PageAllocPort())
  heap: Out(HeapPort())

  def __init__(self, *, n_pages: int, reserved_pages: int = 0):
    n_words = n_pages * PAGE_SIZE
    assert 0 < n_words <= ADDR_MAX + 1
    assert 0 <= reserved_pages <= n_pages

    self.n_pages = n_pages
    self.reserved_pages = reserved_pages
    self.empty_id = Signal(range(n_pages + 1), init=reserved_pages)
    self.free_head_id = Signal(PageId)
    self.free_count = Signal(range(n_pages + 1))
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m, pages = Module(), self
    m.d.sync += [
      pages.done.eq(False),
      pages.oom.eq(False),
    ]
    expand(m, lambda f: program(f, pages))

    with m.If(pages.empty_id > pages.stat_peak):
      m.d.sync += pages.stat_peak.eq(pages.empty_id)
    with m.If(pages.stat_reset):
      m.d.sync += [
        pages.stat_allocs.eq(0),
        pages.stat_frees.eq(0),
        pages.stat_peak.eq(0),
      ]

    return m

def program(f: Flow, pages: PageAlloc):
  f.d.comb += pages.granted.eq(True)
  available = pages.n_pages - pages.empty_id + pages.free_count

  with f.If(pages.req_alloc > 0):
    requested = f.capture(pages.req_alloc)
    yield
    with f.If(requested > available):
      f.d.sync += [
        pages.done.eq(True),
        pages.oom.eq(True)
      ]
      yield
    with f.Else():
      yield
      # specialize LANES = 1 for less timing bubbles
      # keep this in cause I might change the number of LANES later
      if LANES == 1:
        yield from do_alloc_one(f, pages)
      else:
        yield from do_alloc_many(f, pages, requested)

  with f.Elif(pages.req_free > 0):
    yield from free(f, pages)

def do_alloc_one(f: Flow, pages: PageAlloc):
  with f.If(pages.free_count != 0):
    yield
    # prioritize taking entries from the free list
    f.d.sync += pages.alloc_ids[0].eq(pages.free_head_id)
    free_head_base = pages.free_head_id * PAGE_SIZE
    next_free_head_id = yield HeapPort.read(f, pages.heap, addr=free_head_base)
    yield

    f.d.sync += [
      pages.free_head_id.eq(next_free_head_id),
      pages.free_count.eq(pages.free_count - 1),
      pages.stat_allocs.eq(pages.stat_allocs + 1),
      pages.done.eq(True),
    ]
    yield

  with f.Else():
    # take from the bump allocator
    f.d.sync += [
      pages.alloc_ids[0].eq(pages.empty_id),
      pages.empty_id.eq(pages.empty_id + 1),
      pages.stat_allocs.eq(pages.stat_allocs + 1),
      pages.done.eq(True),
    ]
    yield

def do_alloc_many(f: Flow, pages: PageAlloc, requested):
  left_to_alloc = f.capture(requested)

  with f.While((left_to_alloc != 0) & (pages.free_count != 0)):
    yield
    lane_id = (left_to_alloc - 1).as_unsigned()
    f.d.sync += pages.alloc_ids[lane_id].eq(pages.free_head_id)
    free_head_base = pages.free_head_id * PAGE_SIZE
    next_free_head_id = yield HeapPort.read(f, pages.heap, addr=free_head_base)
    yield

    f.d.sync += [
      pages.free_head_id.eq(next_free_head_id),
      left_to_alloc.eq(left_to_alloc - 1),
      pages.free_count.eq(pages.free_count - 1),
      pages.stat_allocs.eq(pages.stat_allocs + 1),
    ]
    yield

  with f.If(left_to_alloc != 0):
    for lane in range(LANES):
      lane_page_id = pages.alloc_ids[lane]
      f.d.sync += lane_page_id.eq(
        Mux(lane < left_to_alloc, pages.empty_id + lane, lane_page_id),
      )
    f.d.sync += [
      pages.empty_id.eq(pages.empty_id + left_to_alloc),
      pages.stat_allocs.eq(pages.stat_allocs + left_to_alloc),
    ]

  f.d.sync += pages.done.eq(True)
  yield

def free(f: Flow, pages: PageAlloc):
  left_to_free = f.capture(pages.req_free)
  page_ids = f.capture(pages.free_ids)
  yield

  with f.While(left_to_free != 0):
    yield
    page_id = page_ids[(left_to_free - 1).as_unsigned()]
    page_base = page_id * PAGE_SIZE

    yield HeapPort.write(f, pages.heap, addr=page_base, write=pages.free_head_id)
    f.d.sync += [
      pages.stat_frees.eq(pages.stat_frees + 1),
      pages.free_head_id.eq(page_id),
      pages.free_count.eq(pages.free_count + 1),
      left_to_free.eq(left_to_free - 1),
      pages.done.eq(left_to_free == 1),
    ]
    yield


PageAllocArbiter = make_arbiter_type(
  PageAllocPort,
  lambda client: (client.req_alloc != 0) | (client.req_free != 0),
  register_grant=True,
)
