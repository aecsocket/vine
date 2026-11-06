from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout, Struct
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike, expand
from ivga0.mem.heap import HeapPort
from ivga0.mem.page_alloc import PAGE_SHIFT, PAGE_SIZE, PageAllocPort, PageId
from ivga0.util.arbiter import make_arbiter_type

from ivga0 import WORD_WIDTH, Addr, Port, Stat, flatten

ALLOC_LANES = 4
"""How many pairs (2 words) can be allocated at once."""

FREE_LANES = 2
"""How many single words can be freed at once."""

PAIR_SHIFT = PAGE_SHIFT - 1
PAIRS_PER_PAGE = PAGE_SIZE // 2 - 1
"""The first pair is reserved for page metadata."""

PageRef = unsigned(PageId.width + 1)
PAGE_NONE = 1 << PageId.width
_HEADER_BITS = PAIR_SHIFT * 2 + (PAIR_SHIFT + 1) + PageRef.width
assert PAIRS_PER_PAGE >= ALLOC_LANES and _HEADER_BITS <= WORD_WIDTH


class PageHeader(Struct):
  """Stored in word zero; word one holds the previous partial-page reference.

  Slot zero is metadata/the local-list sentinel. A half-freed pair still counts
  as used; only fully freed pairs enter the page-local free list.
  """

  used: unsigned(PAIR_SHIFT)
  next_pair: unsigned(PAIR_SHIFT + 1)
  free_head: unsigned(PAIR_SHIFT)
  next_page: PageRef
  padding: unsigned(WORD_WIDTH - _HEADER_BITS)


class PairAllocPort(wiring.Signature):
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
    count = Value.cast(n_pairs)
    f.d.comb += alloc.req_alloc.eq(Mux(accepted, 0, count))
    f.d.sync += [
      accepted.eq(accepted | alloc.granted),
      Assert(accepted | ((count >= 1) & (count <= ALLOC_LANES)), "Invalid pair allocation count"),
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
    count = Value.cast(n_words)
    f.d.comb += [
      alloc.req_free.eq(Mux(accepted, 0, count)),
      alloc.free_addrs.eq(addrs),
    ]
    f.d.sync += [
      accepted.eq(accepted | alloc.granted),
      Assert(accepted | ((count >= 1) & (count <= FREE_LANES)), "Invalid pair free count"),
    ]
    f.d.until += accepted & alloc.done
    f.d.on_done += accepted.eq(False)

  @staticmethod
  def free(f: Flow, alloc, *, addr: ValueLike) -> None:
    PairAllocPort.free_all(f, alloc, n_words=1, addrs=addr)


class PairAlloc(wiring.Component):
  """Subdivide shared pages into pairs and return completely empty pages.

  Wire `pages` to PageAlloc and arbitrate both allocators' heap ports. Allocated
  words are not initialized: callers must replace both old words before use/free.
  `free_count` counts reusable words in owned pages, including unissued slots;
  `live_words` counts outstanding individual words, including stranded halves.
  Allocation/free stats count words; `stat_peak` is a live-word high watermark.
  """

  control: In(PairAllocPort())
  pages: Out(PageAllocPort())
  heap: Out(HeapPort())

  def __init__(self):
    self.partial_head = Signal(PageRef, init=PAGE_NONE)
    self.pages_owned = Signal(range((1 << PageId.width) + 1))
    self.free_count = Signal(Addr)
    self.live_words = Signal(Addr)
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += [self.done.eq(False), self.oom.eq(False)]
    expand(m, lambda f: program(f, self))

    with m.If(self.live_words > self.stat_peak):
      m.d.sync += self.stat_peak.eq(self.live_words)
    with m.If(self.stat_reset):
      m.d.sync += [
        self.stat_allocs.eq(0),
        self.stat_frees.eq(0),
        self.stat_peak.eq(0),
      ]
    return m


def program(f: Flow, pairs: PairAlloc):
  f.d.comb += pairs.granted.eq(True)
  with f.If(pairs.req_alloc != 0):
    f.d.sync += Assert(pairs.req_alloc <= ALLOC_LANES, "Allocation request exceeds lane count")
    yield from do_alloc(f, pairs)
  with f.Elif(pairs.req_free != 0):
    f.d.sync += Assert(pairs.req_free <= FREE_LANES, "Free request exceeds lane count")
    yield from free(f, pairs)


def write_header(f: Flow, pairs: PairAlloc, page, old, **fields):
  updated = Signal(PageHeader)
  f.d.comb += updated.eq(old)
  for name, value in fields.items():
    f.d.comb += getattr(updated, name).eq(value)
  yield HeapPort.write(f, pairs.heap, addr=page << PAGE_SHIFT, write=updated.as_value())


def write_previous(f: Flow, pairs: PairAlloc, page, previous):
  yield HeapPort.write(f, pairs.heap, addr=(page << PAGE_SHIFT) + 1, write=previous)


def acquire_page(f: Flow, pairs: PairAlloc, failed):
  page_ids, oom = yield PageAllocPort.alloc(f, pairs.pages, n_pages=1)
  with f.If(oom):
    f.d.sync += failed.eq(True)
    yield
  with f.Else():
    yield
    initial = Const({
      "used": 0,
      "next_pair": 1,
      "free_head": 0,
      "next_page": PAGE_NONE,
    }, PageHeader)
    yield from write_previous(f, pairs, page_ids[0], PAGE_NONE)
    yield HeapPort.write(f, pairs.heap, addr=page_ids[0] << PAGE_SHIFT, write=initial.as_value())
    f.d.sync += [
      pairs.partial_head.eq(page_ids[0]),
      pairs.pages_owned.eq(pairs.pages_owned + 1),
      pairs.free_count.eq(pairs.free_count + PAIRS_PER_PAGE * 2),
    ]
    yield


def commit_pair(f: Flow, pairs: PairAlloc, remaining, page, header, index, *, free_head, next_pair):
  yield from write_header(
    f, pairs, page, header,
    used=header.used + 1, free_head=free_head, next_pair=next_pair,
  )
  f.d.sync += [
    pairs.alloc_bases[(remaining - 1).as_unsigned()].eq((page << PAGE_SHIFT) + index * 2),
    pairs.free_count.eq(pairs.free_count - 2),
    remaining.eq(remaining - 1),
  ]
  yield
  with f.If(header.used == PAIRS_PER_PAGE - 1):
    yield
    yield from unlink_page(f, pairs, page, header)


def reserve_pair(f: Flow, pairs: PairAlloc, remaining):
  page = f.capture(pairs.partial_head)
  raw = yield HeapPort.read(f, pairs.heap, addr=page << PAGE_SHIFT)
  yield
  header = PageHeader(raw)
  index = f.capture(Mux(header.free_head != 0, header.free_head, header.next_pair))
  f.d.sync += [
    Assert(header.used < PAIRS_PER_PAGE, "Allocating from a full node page"),
    Assert((index >= 1) & (index <= PAIRS_PER_PAGE), "Invalid page-local pair index"),
  ]
  with f.If(header.free_head != 0):
    yield
    next_free = yield HeapPort.read(f, pairs.heap, addr=(page << PAGE_SHIFT) + index * 2)
    yield
    yield from commit_pair(
      f, pairs, remaining, page, header, index,
      free_head=next_free[:PAIR_SHIFT], next_pair=header.next_pair,
    )
  with f.Else():
    yield
    yield from commit_pair(
      f, pairs, remaining, page, header, index,
      free_head=0, next_pair=header.next_pair + 1,
    )


def do_alloc(f: Flow, pairs: PairAlloc):
  requested = f.capture(pairs.req_alloc)
  remaining = f.capture(pairs.req_alloc)
  failed = f.capture(False)
  yield

  with f.While((remaining != 0) & ~failed):
    yield
    with f.If(pairs.partial_head == PAGE_NONE):
      yield
      yield from acquire_page(f, pairs, failed)
    with f.If(~failed):
      yield
      yield from reserve_pair(f, pairs, remaining)

  with f.If(failed):
    yield
    # Reservations are private until done. Undo them without counting user frees.
    undo = f.capture(remaining)
    with f.While(undo < requested):
      yield
      # Aborted reservations were never initialized by the caller. Restore the
      # odd-half marker before publishing the even half as a free-list link.
      yield HeapPort.write(
        f, pairs.heap,
        addr=Addr.other_half(pairs.alloc_bases[undo]),
        write=Port.freed().as_value(),
      )
      yield from release_pair(f, pairs, pairs.alloc_bases[undo])
      f.d.sync += undo.eq(undo + 1)
      yield
    f.d.sync += [pairs.alloc_bases.eq(0), pairs.oom.eq(True)]
  with f.Else():
    f.d.sync += [
      pairs.stat_allocs.eq(pairs.stat_allocs + requested * 2),
      pairs.live_words.eq(pairs.live_words + requested * 2),
    ]
  f.d.sync += pairs.done.eq(True)
  yield


def unlink_page(f: Flow, pairs: PairAlloc, page, header):
  with f.If(pairs.partial_head == page):
    yield
    with f.If(header.next_page != PAGE_NONE):
      yield
      yield from write_previous(f, pairs, header.next_page, PAGE_NONE)
      f.d.sync += pairs.partial_head.eq(header.next_page)
      yield
    with f.Else():
      f.d.sync += pairs.partial_head.eq(PAGE_NONE)
      yield
  with f.Else():
    yield
    previous_raw = yield HeapPort.read(f, pairs.heap, addr=(page << PAGE_SHIFT) + 1)
    yield
    previous = previous_raw[:PageRef.width]
    f.d.sync += Assert(previous < PAGE_NONE, "Invalid previous partial-page reference")
    raw = yield HeapPort.read(f, pairs.heap, addr=previous << PAGE_SHIFT)
    yield
    previous_header = PageHeader(raw)
    f.d.sync += Assert(previous_header.next_page == page, "Broken partial-page back link")
    yield from write_header(f, pairs, previous, previous_header, next_page=header.next_page)
    with f.If(header.next_page != PAGE_NONE):
      yield
      yield from write_previous(f, pairs, header.next_page, previous)


def release_pair(f: Flow, pairs: PairAlloc, addr):
  page = f.capture(addr >> PAGE_SHIFT)
  index = f.capture(addr[1:PAGE_SHIFT])
  raw = yield HeapPort.read(f, pairs.heap, addr=page << PAGE_SHIFT)
  yield
  header = PageHeader(raw)
  f.d.sync += [
    Assert(index != 0, "Cannot free page metadata"),
    Assert(header.used != 0, "Freeing a pair from an empty page"),
  ]

  with f.If(header.used == 1):
    yield
    yield from unlink_page(f, pairs, page, header)
    yield PageAllocPort.free(f, pairs.pages, n_pages=1, page_ids=page)
    f.d.sync += [
      pairs.pages_owned.eq(pairs.pages_owned - 1),
      pairs.free_count.eq(pairs.free_count - (PAIRS_PER_PAGE - 1) * 2),
    ]
    yield
  with f.Else():
    yield
    yield HeapPort.write(
      f, pairs.heap,
      addr=(page << PAGE_SHIFT) + index * 2,
      write=header.free_head,
    )
    with f.If(header.used == PAIRS_PER_PAGE):
      yield
      yield from write_previous(f, pairs, page, PAGE_NONE)
      with f.If(pairs.partial_head != PAGE_NONE):
        yield
        yield from write_previous(f, pairs, pairs.partial_head, page)
    next_page = Mux(header.used == PAIRS_PER_PAGE, pairs.partial_head, header.next_page)
    yield from write_header(
      f, pairs, page, header,
      used=header.used - 1, free_head=index, next_page=next_page,
    )
    with f.If(header.used == PAIRS_PER_PAGE):
      f.d.sync += pairs.partial_head.eq(page)
    f.d.sync += pairs.free_count.eq(pairs.free_count + 2)
    yield


def free(f: Flow, pairs: PairAlloc):
  remaining = f.capture(pairs.req_free)
  addrs = f.capture(pairs.free_addrs)
  yield
  freed = Port.freed().as_value()

  with f.While(remaining != 0):
    yield
    addr = addrs[(remaining - 1).as_unsigned()]
    f.d.sync += Assert(addr[1:PAGE_SHIFT] != 0, "Cannot free page metadata")
    other = HeapPort.read(f, pairs.heap, addr=Addr.other_half(addr))
    yield HeapPort.write(f, pairs.heap, addr=addr, write=freed)
    yield

    with f.If(other == freed):
      yield
      yield from release_pair(f, pairs, addr)

    f.d.sync += Assert(pairs.live_words != 0, "Freeing without a live word")
    f.d.on_done += [
      pairs.stat_frees.eq(pairs.stat_frees + 1),
      pairs.live_words.eq(pairs.live_words - 1),
      remaining.eq(remaining - 1),
      pairs.done.eq(remaining == 1),
    ]
    yield


PairAllocArbiter = make_arbiter_type(
  PairAllocPort,
  lambda client: (client.req_alloc != 0) | (client.req_free != 0),
  register_grant=True,
)
