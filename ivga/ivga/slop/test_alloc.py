"""Page-backed paired allocator integration tests.

From ivga/: python -m ivga.slop.test_alloc

Use the real PageAlloc, Heap and HeapArbiter. The oracle tracks individual word
ownership, independently walks raw page headers/local free lists, and checks
that every empty page has returned to the shared page budget at completion.
Allocated words are initialized by the caller, never by the allocator.

A gate between the heap arbiter and heap supplies deterministic backpressure
and poisons read data outside response cycles. Completion is bounded, not tied
to the obsolete global-bump allocator's minimum-cycle schedule. Comparing the
accepted transaction traces with/without stalls detects duplicate transactions.
"""

import random
from collections import Counter
from dataclasses import dataclass

from amaranth import Module, Mux, Signal
from amaranth.lib import wiring
from amaranth.sim import Simulator

from ivga import ADDR_MAX, Port
from ivga.mem.heap import CAPACITY, Heap, HeapArbiter
from ivga.mem.page_alloc import PAGE_SIZE, PageAlloc, PageAllocArbiter, PageId
from ivga.mem.pair_alloc import ALLOC_LANES, FREE_LANES, PageHeader, PairAlloc

PERIOD = 1e-6
NONE = 1 << PageId.width
PAIRS_PER_PAGE = PAGE_SIZE // 2 - 1
FREED = Port.freed().as_value().value
POISON = 0xDEAD_BEEF_CAFE_F00D
GRANTS = ((True,), (False, True), (True, False, False, True, False))


def words(bases):
  return tuple(addr for base in bases for addr in (base, base ^ 1))


def payload(ctx, port):
  read, write = ctx.get(port.read), ctx.get(port.write)
  if not (read or write):
    return None
  return (ctx.get(port.read_addr) if read else None,
          ctx.get(port.write_addr) if write else None,
          ctx.get(port.write_data) if write else None)


@dataclass(frozen=True)
class Transaction:
  client: str
  read_addr: int | None
  write_addr: int | None
  data: int | None


class System:
  def __init__(self, n_pages, *, shared=False):
    self.n_pages = n_pages
    self.m = m = Module()
    self.pages = m.submodules.pages = PageAlloc(n_pages)
    self.pairs = m.submodules.pairs = PairAlloc()
    self.heap = m.submodules.heap = Heap()
    self.arb = m.submodules.heap_arb = HeapArbiter(2)
    wiring.connect(m, self.pages.heap, self.arb.clients[0])
    wiring.connect(m, self.pairs.heap, self.arb.clients[1])
    self.other = None
    if shared:
      page_arb = m.submodules.page_arb = PageAllocArbiter(2)
      wiring.connect(m, self.pairs.pages, page_arb.clients[0])
      wiring.connect(m, page_arb.real, self.pages.control)
      self.other = page_arb.clients[1]
    else:
      wiring.connect(m, self.pairs.pages, self.pages.control)

    # Denial must suppress BOTH downstream enables, not just upstream granted:
    # Heap commits whenever its read/write inputs are asserted.
    self.allow = Signal()
    self.response = Signal()
    real, heap = self.arb.real, self.heap
    m.d.comb += [
      heap.read.eq(real.read & self.allow),
      heap.write.eq(real.write & self.allow),
      heap.read_addr.eq(real.read_addr),
      heap.write_addr.eq(real.write_addr),
      heap.write_data.eq(real.write_data),
      real.granted.eq(heap.granted & self.allow),
      real.read_data.eq(Mux(self.response, heap.read_data, POISON)),
    ]
    m.d.sync += self.response.eq(heap.read & heap.granted)


class Harness:
  def __init__(self, ctx, system, grants):
    assert (ALLOC_LANES, FREE_LANES, PAGE_SIZE, PAIRS_PER_PAGE) == (4, 2, 128, 63)
    assert any(grants)
    self.ctx, self.system = ctx, system
    self.dut, self.pages = system.pairs, system.pages
    self.grants = grants
    self.cycle = self.stalls = self.poisoned = 0
    self.trace = []
    self.denied = {}
    self.live = set()
    self.live_data = {}
    self.foreign = set()
    self.next_pairs = {}
    self.allocs = self.frees = self.peak = self.lifetime = 0
    self.memory = [0] * CAPACITY
    for addr in range(system.n_pages * PAGE_SIZE):
      self.store(addr, POISON ^ (addr * 0x10001))
    self.check()

  def store(self, addr, value):
    """Application/debug access, not an allocator heap transaction."""
    self.ctx.set(self.system.heap.memory.data[addr], value)
    self.memory[addr] = value

  def header(self, page):
    return PageHeader.from_bits(self.memory[page * PAGE_SIZE])

  @property
  def owned(self):
    return {addr // PAGE_SIZE for addr in self.live}

  @property
  def available(self):
    occupied = len({addr & ~1 for addr in self.live})
    return (self.system.n_pages - len(self.foreign)) * PAIRS_PER_PAGE - occupied

  def partial(self):
    result, page = [], self.ctx.get(self.dut.partial_head)
    previous = NONE
    while page != NONE:
      assert page in self.owned and page not in result, ("partial list cycle/ownership", result, page)
      assert self.memory[page * PAGE_SIZE + 1] == previous, ("partial back link", page, previous)
      result.append(page)
      previous, page = page, self.header(page).next_page
    return result

  def check(self, *, advance=0):
    ctx, dut = self.ctx, self.dut
    owned = self.owned
    assert not owned & self.foreign
    assert ctx.get(dut.live_words) == len(self.live), ("live_words", ctx.get(dut.live_words), len(self.live))
    assert ctx.get(dut.pages_owned) == len(owned), ("pages_owned", ctx.get(dut.pages_owned), owned)
    reusable, partial = 0, set()
    next_pairs = {}
    for page in owned:
      header = self.header(page)
      used = {addr % PAGE_SIZE // 2 for addr in self.live if addr // PAGE_SIZE == page}
      assert 0 not in used, "allocated reserved header pair"
      assert header.used == len(used), ("header.used", page, header.used, used)
      old_next = self.next_pairs.get(page, 1)
      assert max(used) < header.next_pair <= PAIRS_PER_PAGE + 1, ("next_pair", page, header.next_pair)
      assert old_next <= header.next_pair <= old_next + advance, ("bump progress", page, old_next, header.next_pair, advance)
      next_pairs[page] = header.next_pair
      free, local = set(), header.free_head
      while local:
        assert 1 <= local < header.next_pair and local not in free, ("local list cycle/range", page, local, free)
        assert local not in used, ("live pair on free list", page, local)
        free.add(local)
        base = page * PAGE_SIZE + local * 2
        assert self.memory[base ^ 1] == FREED, ("free pair odd marker", base)
        # Intentionally RAW local pair indices, not Port.with_addr encodings.
        local = self.memory[base]
      assert free == set(range(1, header.next_pair)) - used, ("lost/free pair", page, free, used)
      for local in used:
        base = page * PAGE_SIZE + local * 2
        for addr in (base, base ^ 1):
          if addr not in self.live:
            assert self.memory[addr] == FREED, ("half-free marker", addr)
      reusable += 2 * (PAIRS_PER_PAGE - len(used))
      if len(used) < PAIRS_PER_PAGE:
        partial.add(page)
    assert set(self.partial()) == partial, ("partial membership", self.partial(), partial)
    assert ctx.get(dut.free_count) == reusable, ("reusable words", ctx.get(dut.free_count), reusable)
    self.next_pairs = next_pairs
    for addr in self.live:
      assert self.memory[addr] == self.live_data[addr], ("live data overwritten", addr)

    # PageAlloc has a count-terminated raw page-id free list (not PairAlloc's
    # extended-id sentinel). Include unissued bump pages in the total budget.
    empty, count = ctx.get(self.pages.empty_id), ctx.get(self.pages.free_count)
    assert 0 <= count <= empty <= self.system.n_pages
    returned, page = set(), ctx.get(self.pages.free_head_id)
    for _ in range(count):
      assert 0 <= page < empty and page not in returned, ("page free list", page, returned)
      returned.add(page)
      page = self.memory[page * PAGE_SIZE]
    assert returned.isdisjoint(owned | self.foreign)
    assert set(range(empty)) == returned | owned | self.foreign, ("leaked page budget", empty, returned, owned, self.foreign)
    assert ctx.get(self.pages.stat_allocs) - ctx.get(self.pages.stat_frees) == len(owned | self.foreign)
    assert (ctx.get(dut.stat_allocs), ctx.get(dut.stat_frees)) == (self.allocs, self.frees), "word statistics"
    for addr, value in enumerate(self.memory[:self.system.n_pages * PAGE_SIZE]):
      assert ctx.get(self.system.heap.memory.data[addr]) == value, ("heap/shadow mismatch", addr)

  async def tick(self, *, reset=False):
    ctx, system = self.ctx, self.system
    ctx.set(system.allow, self.grants[self.cycle % len(self.grants)])
    ctx.set(self.dut.stat_reset, reset)
    accepted = []
    for name, port in (("pages", self.pages.heap), ("pairs", self.dut.heap)):
      request = payload(ctx, port)
      if name in self.denied:
        assert request == self.denied[name], ("denied payload changed/withdrawn", name, self.denied[name], request)
      self.denied.pop(name, None)
      if request is not None:
        if ctx.get(port.granted):
          accepted.append(Transaction(name, *request))
        else:
          self.denied[name] = request
          self.stalls += 1
    actual = payload(ctx, system.heap)
    assert len(accepted) == int(actual is not None), ("gate/arbiter duplicated or lost grant", accepted, actual)
    expected_read = None
    if accepted:
      transaction = accepted[0]
      assert (transaction.read_addr, transaction.write_addr, transaction.data) == actual
      for addr in (transaction.read_addr, transaction.write_addr):
        assert addr is None or 0 <= addr < system.n_pages * PAGE_SIZE, ("heap address", addr)
      if transaction.read_addr is not None:
        expected_read = self.memory[transaction.read_addr]
      if transaction.write_addr is not None:
        self.memory[transaction.write_addr] = transaction.data
      self.trace.append(transaction)
    await ctx.tick()
    self.cycle += 1
    if expected_read is not None:
      assert ctx.get(system.arb.real.read_data) == expected_read, "read-first heap response"
      port = self.pages.heap if accepted[0].client == "pages" else self.dut.heap
      assert ctx.get(port.read_data) == expected_read, "heap response routed to wrong owner"
    else:
      assert ctx.get(system.arb.real.read_data) == POISON, "missing read-data poison"
    if reset:
      assert all(ctx.get(signal) == 0 for signal in (self.dut.stat_allocs, self.dut.stat_frees, self.dut.stat_peak)), "reset priority"
    return bool(ctx.get(self.dut.done))

  async def idle(self, cycles=1, *, reset=False):
    ctx, dut = self.ctx, self.dut
    ctx.set(dut.req_alloc, 0)
    ctx.set(dut.req_free, 0)
    start = len(self.trace)
    for _ in range(cycles):
      assert not await self.tick(reset=reset), "done must be a pulse"
      assert ctx.get(dut.granted) and not ctx.get(dut.oom)
      if reset:
        self.allocs = self.frees = self.peak = 0
      else:
        self.peak = max(self.peak, len(self.live))
      assert ctx.get(dut.stat_peak) == self.peak, ("live highwater", ctx.get(dut.stat_peak), self.peak)
    assert len(self.trace) == start, "idle allocator issued a heap transaction"
    assert not self.denied
    self.check()

  async def operation(self, kind, count=0, addresses=(), *, reset=False, poison=True):
    ctx, dut = self.ctx, self.dut
    assert ctx.get(dut.granted), "serialized caller requires ready"
    if kind == "free":
      assert 1 <= len(addresses) <= FREE_LANES
      assert len(set(addresses)) == len(addresses) and set(addresses) <= self.live
    else:
      assert 1 <= count <= ALLOC_LANES
    oom = kind == "alloc" and count > self.available
    before_live, before_owned = set(self.live), set(self.owned)
    start = len(self.trace)
    ctx.set(dut.req_alloc, count if kind == "alloc" else 0)
    ctx.set(dut.req_free, len(addresses))
    ctx.set(dut.free_addrs, list(addresses) + [ADDR_MAX] * (FREE_LANES - len(addresses)))
    # The accepting edge need not finish; done is a registered completion pulse.
    assert not ctx.get(dut.done), "completion must be registered, not combinational"
    complete = await self.tick(reset=reset)
    for _ in range(4096):
      if complete:
        break
      assert not ctx.get(dut.granted), "busy allocator accepted a second request"
      ctx.set(dut.req_alloc, 7 if poison else 0)
      ctx.set(dut.req_free, 3 if poison else 0)
      ctx.set(dut.free_addrs, [ADDR_MAX, 0])
      self.poisoned += int(poison)
      complete = await self.tick(reset=reset)
    else:
      raise AssertionError(f"{kind} timed out after 4096 edges")
    ctx.set(dut.req_alloc, 0)
    ctx.set(dut.req_free, 0)
    ctx.set(dut.stat_reset, 0)
    assert bool(ctx.get(dut.oom)) == oom, ("OOM availability", kind, count, self.available)
    result = ()
    if kind == "alloc":
      if oom:
        assert all(ctx.get(dut.alloc_bases[i]) == 0 for i in range(ALLOC_LANES)), "OOM exposed reservations"
        assert self.live == before_live and self.owned == before_owned
      else:
        result = tuple(ctx.get(dut.alloc_bases[i]) for i in range(count))
        allocated = set(words(result))
        assert len(allocated) == 2 * count and not allocated & self.live, ("overlapping allocation", result)
        assert all(0 <= base < self.system.n_pages * PAGE_SIZE and base % 2 == 0 and base % PAGE_SIZE >= 2 for base in result), ("pair base", result)
        assert not {base // PAGE_SIZE for base in result} & self.foreign
        assert not any(tx.client == "pairs" and tx.write_addr in allocated
                       for tx in self.trace[start:]), "allocator initialized application words"
        self.live.update(allocated)
        self.allocs += 2 * count
        # Initialize BOTH halves after successful completion. A reused free-list
        # link/FREED marker must not survive into the next application lifetime.
        self.lifetime += 1
        for addr in allocated:
          value = Port.comb(addr, self.lifetime).as_value().value
          assert value != FREED
          self.store(addr, value)
          self.live_data[addr] = value
    else:
      self.live.difference_update(addresses)
      self.frees += len(addresses)
      stamps = Counter(tx.write_addr for tx in self.trace[start:]
                       if tx.client == "pairs" and tx.data == FREED)
      assert stamps == Counter(addresses), ("missing/duplicated free stamp", addresses, stamps)
    if reset:
      self.allocs = self.frees = self.peak = 0
    self.check(advance=count if kind == "alloc" else 0)
    # Allow the registered peak comparison to observe final live_words; no
    # minimum-cycle assumption about the metadata protocol is made here.
    if not reset:
      self.peak = max(self.peak, len(self.live))
    await self.idle()
    return result

  async def alloc(self, count, **kwargs):
    return await self.operation("alloc", count=count, **kwargs)

  async def free(self, addresses, **kwargs):
    _ = await self.operation("free", addresses=tuple(addresses), **kwargs)

  async def fill(self, count):
    result = []
    while count:
      batch = min(count, ALLOC_LANES)
      result.extend(await self.alloc(batch))
      count -= batch
    return result

  async def release_pairs(self, bases):
    for base in bases:
      await self.free((base, base ^ 1))

  async def other_page(self, page=None):
    """A second, serialized PageAlloc client shares exactly the same budget."""
    ctx, port = self.ctx, self.system.other
    assert port is not None
    allocating = page is None
    if not allocating:
      assert page in self.foreign
      ctx.set(port.free_ids, [page])
    oom = allocating and len(self.owned | self.foreign) == self.system.n_pages
    ctx.set(port.req_alloc, int(allocating))
    ctx.set(port.req_free, int(not allocating))
    for _ in range(4096):
      granted = bool(ctx.get(port.granted))
      assert not await self.tick(), "foreign page operation completed a pair request"
      if granted:
        ctx.set(port.req_alloc, 0)
        ctx.set(port.req_free, 0)
        break
    else:
      raise AssertionError("foreign page request not granted")
    for _ in range(4096):
      if ctx.get(port.done):
        break
      assert not await self.tick()
    else:
      raise AssertionError("foreign page request did not complete")
    assert bool(ctx.get(port.oom)) == oom
    if allocating and not oom:
      page = ctx.get(port.alloc_ids[0])
      assert page not in self.owned | self.foreign
      self.foreign.add(page)
    elif not allocating:
      self.foreign.remove(page)
    self.check()
    await self.idle()
    return None if oom else page


def simulate(n_pages, scenario, *, grants=(True,), shared=False):
  system = System(n_pages, shared=shared)
  sim = Simulator(system.m)
  sim.add_clock(PERIOD)
  result = []

  async def bench(ctx):
    h = Harness(ctx, system, grants)
    assert ctx.get(h.dut.granted) and not ctx.get(h.dut.done)
    assert ctx.get(h.dut.partial_head) == NONE
    await scenario(h)
    await h.idle(2)
    result.append((tuple(h.trace), h.stalls, h.poisoned))

  sim.add_testbench(bench)
  sim.run()
  return result[0]


def test_page_boundaries():
  async def scenario(h):
    first = await h.fill(62)
    assert set(first) == set(range(2, 126, 2)), "first page must be id zero, with reserved header"
    crossing = await h.alloc(4)
    assert set(crossing) == {126, 130, 132, 134}
    assert h.header(0).used == 63 and h.header(0).next_pair == 64
    assert h.partial() == [1]
    rest = await h.fill(60)
    assert set(first + list(crossing) + rest) == {
      page * PAGE_SIZE + offset for page in range(2) for offset in range(2, PAGE_SIZE, 2)
    }
    assert h.ctx.get(h.dut.free_count) == 0 and h.partial() == []
    assert await h.alloc(1) == ()
    await h.release_pairs(first + list(crossing) + rest)
    assert not h.owned and h.ctx.get(h.pages.free_count) == 2
  simulate(2, scenario)


def test_half_frees_local_reuse_and_raw_links():
  async def scenario(h):
    a, b, c, d, e, anchor = await h.fill(6)
    available = h.ctx.get(h.dut.free_count)
    await h.free((a ^ 1,))
    await h.free((b,))
    assert h.ctx.get(h.dut.free_count) == available, "half pairs are not reusable"
    await h.free((a,))
    await h.free((b ^ 1,))
    header = h.header(0)
    assert header.free_head == b // 2
    assert h.memory[b] == a // 2 and h.memory[a] == 0, "links must be raw local indices"
    await h.free((c ^ 1, c))
    await h.free((d, e ^ 1))
    await h.free((e, d ^ 1))
    assert h.ctx.get(h.dut.free_count) == available + 10
    reused = await h.fill(5)
    assert set(reused) == {a, b, c, d, e}, "local free pairs must precede unused bump space"
    assert h.header(0).next_pair == 7
    # Reinitialize-on-allocation is essential: a stale mate marker would make
    # this first half free incorrectly publish a whole pair again.
    await h.free((reused[0] ^ 1,))
    assert h.header(0).used == 6
    await h.free((reused[0],))
    await h.release_pairs(reused[1:] + [anchor])
    assert not h.owned
  simulate(1, scenario, grants=(False, False, True, False, True))


def test_multiple_partial_pages_and_full_to_partial():
  async def scenario(h):
    bases = await h.fill(3 * PAIRS_PER_PAGE)
    assert h.partial() == []
    per_page = [[base for base in bases if base // PAGE_SIZE == page] for page in range(3)]
    released = []
    for page in range(3):
      base = per_page[page][0]
      await h.free((base, base ^ 1))
      released.append(base)
      assert h.partial() == list(range(page, -1, -1))
    extra = per_page[0][1]
    await h.free((extra ^ 1, extra))
    released.append(extra)
    assert h.partial() == [2, 1, 0], "already-partial page must not be inserted twice"
    assert set(await h.alloc(4)) == set(released)
    assert h.partial() == [] and h.ctx.get(h.dut.free_count) == 0
  simulate(3, scenario, grants=(True, False, True))


def test_empty_page_reclamation_and_non_head_unlink():
  async def scenario(h):
    bases = await h.fill(3 * PAIRS_PER_PAGE)
    anchors = []
    for page in range(3):
      local = [base for base in bases if base // PAGE_SIZE == page]
      anchors.append(local[0])
      await h.release_pairs(local[1:])
    assert h.partial() == [2, 1, 0]
    await h.free((anchors[1] ^ 1, anchors[1]))
    assert h.partial() == [2, 0], "empty middle page must reconnect both neighbors"
    assert h.ctx.get(h.pages.free_count) == 1
    await h.free((anchors[0], anchors[0] ^ 1))
    assert h.partial() == [2], "empty tail page must be unlinked"
    assert h.ctx.get(h.pages.free_count) == 2
    await h.free((anchors[2],))
    assert h.ctx.get(h.dut.pages_owned) == 1, "last half still owns the page"
    await h.free((anchors[2] ^ 1,))
    assert h.partial() == [] and h.ctx.get(h.dut.pages_owned) == 0
    assert h.ctx.get(h.dut.free_count) == 0 and h.ctx.get(h.pages.free_count) == 3
    recycled = await h.alloc(1)
    assert recycled[0] % PAGE_SIZE == 2
    assert h.header(recycled[0] // PAGE_SIZE).next_pair == 2, "reacquired page needs a fresh header"
  simulate(3, scenario, grants=(False, True, False, False, True))


def test_unlink_does_not_scan():
  async def scenario(h):
    bases = await h.fill(h.system.n_pages * PAIRS_PER_PAGE)
    local = [base for base in bases if base // PAGE_SIZE == 0]
    await h.release_pairs(local[1:])
    for page in range(1, h.system.n_pages):
      base = next(base for base in bases if base // PAGE_SIZE == page)
      await h.free((base, base ^ 1))
    assert h.partial() == list(reversed(range(h.system.n_pages)))

    start = len(h.trace)
    await h.free((local[0], local[0] ^ 1))
    metadata_reads = [tx.read_addr for tx in h.trace[start:]
                      if tx.client == "pairs" and tx.read_addr is not None
                      and tx.read_addr % PAGE_SIZE < 2]
    # Target header + its previous-page word + predecessor header, at any depth.
    assert metadata_reads == [0, 1, PAGE_SIZE], ("unlink scanned the list", metadata_reads)
    assert h.partial() == list(reversed(range(1, h.system.n_pages)))

  for n_pages in (3, 8):
    simulate(n_pages, scenario, grants=(False, True, True, False))


def test_atomic_oom_and_rollback():
  async def scenario(h):
    bases = await h.fill(60)
    before = (set(h.live), h.allocs, h.frees, h.peak)
    assert h.ctx.get(h.dut.free_count) == 6
    assert await h.alloc(4) == ()
    assert (h.live, h.allocs, h.frees, h.peak) == before
    assert h.ctx.get(h.dut.free_count) == 6, "failed reservations must be reusable"
    tail = await h.alloc(3)
    assert set(tail) == {122, 124, 126}, "rollback lost or duplicated the final pairs"
    assert await h.alloc(1) == ()
    await h.free((bases[0] ^ 1,))
    assert await h.alloc(1) == (), "one freed half cannot satisfy a pair allocation"
    await h.free((bases[0],))
    assert await h.alloc(4) == (), "rollback of a local-list reservation must be atomic"
    assert await h.alloc(1) == (bases[0],)
  simulate(1, scenario, grants=(False, True, True, False))


def test_shared_page_budget():
  async def scenario(h):
    assert await h.other_page() == 0
    bases = await h.fill(PAIRS_PER_PAGE)
    assert {base // PAGE_SIZE for base in bases} == {1}
    assert await h.alloc(1) == (), "foreign-owned pages must not be used"
    await h.other_page(0)
    new = await h.alloc(4)
    assert set(new) == {2, 4, 6, 8}, "returned foreign page must be available to pairs"
    await h.release_pairs(bases + list(new))
    ids = {await h.other_page(), await h.other_page()}
    assert ids == {0, 1}, "pair reclamation must return pages to other clients"
    assert await h.other_page() is None
    assert await h.alloc(4) == ()
    for page in sorted(ids):
      await h.other_page(page)
    assert not h.foreign and not h.owned
  simulate(2, scenario, shared=True, grants=(False, True, False, True))


def test_stalls_poison_and_exact_once_transactions():
  async def scenario(h):
    bases = await h.fill(61)
    assert await h.alloc(4) == ()
    await h.free((bases[2] ^ 1, bases[3]))
    await h.free((bases[2], bases[3] ^ 1))
    reused = await h.alloc(4)
    assert {bases[2], bases[3]} <= set(reused)
    assert await h.alloc(1) == ()
    await h.release_pairs([base for base in bases if base not in (bases[2], bases[3])] + list(reused))
    fresh = await h.alloc(4)
    await h.release_pairs(fresh)
  reference, stalls, poisoned = simulate(1, scenario)
  assert not stalls and poisoned > 0
  for grants in GRANTS[1:]:
    trace, stalls, poisoned = simulate(1, scenario, grants=grants)
    assert trace == reference, "backpressure duplicated, dropped or changed accepted transactions"
    assert stalls > 0 and poisoned > 0


def test_statistics_and_reset_priority():
  async def scenario(h):
    bases = await h.fill(8)
    assert (h.allocs, h.frees, h.peak) == (16, 0, 16)
    await h.release_pairs(bases[:7])
    assert (h.allocs, h.frees, h.peak) == (16, 14, 16)
    await h.idle(reset=True)
    assert h.ctx.get(h.dut.live_words) == 2 and h.ctx.get(h.dut.pages_owned) == 1
    await h.idle()
    assert (h.allocs, h.frees, h.peak) == (0, 0, 2), "peak tracks live words, not bump/page capacity"
    reused = await h.alloc(4, reset=True)
    assert (h.allocs, h.frees, h.peak) == (0, 0, 10)
    await h.free((reused[0] ^ 1,), reset=True)
    assert (h.allocs, h.frees, h.peak) == (0, 0, 9)
    await h.free((reused[0],), reset=True)
    assert (h.allocs, h.frees, h.peak) == (0, 0, 8)
    await h.release_pairs(list(reused[1:]) + [bases[-1]])
    assert (h.allocs, h.frees, h.peak) == (0, 8, 8)
    await h.idle(reset=True)
    assert (h.allocs, h.frees, h.peak) == (0, 0, 0)
    await h.alloc(1)
    assert (h.allocs, h.frees, h.peak) == (2, 0, 2)
  simulate(1, scenario, grants=(False, True, False, True))


def test_randomized_ownership():
  for seed in (0, 17, 2901):
    async def scenario(h, seed=seed):
      rng = random.Random(seed)
      await h.fill(110)
      for _ in range(240):
        if h.live and rng.randrange(5) < 3:
          count = rng.randint(1, min(FREE_LANES, len(h.live)))
          await h.free(rng.sample(sorted(h.live), count))
        else:
          await h.alloc(rng.randint(1, ALLOC_LANES))
      while h.live:
        await h.free(rng.sample(sorted(h.live), min(FREE_LANES, len(h.live))))
      assert h.ctx.get(h.dut.pages_owned) == 0
      assert h.ctx.get(h.pages.free_count) == 2
      await h.fill(2 * PAIRS_PER_PAGE)
      assert h.ctx.get(h.dut.live_words) == 252
      assert h.ctx.get(h.dut.free_count) == 0
      assert await h.alloc(4) == ()
    try:
      simulate(2, scenario, grants=(False, True, False, False, True, True))
    except AssertionError as error:
      raise AssertionError(f"seed={seed}: {error}") from error


def main():
  tests = [
    test_page_boundaries,
    test_half_frees_local_reuse_and_raw_links,
    test_multiple_partial_pages_and_full_to_partial,
    test_empty_page_reclamation_and_non_head_unlink,
    test_unlink_does_not_scan,
    test_atomic_oom_and_rollback,
    test_shared_page_budget,
    test_stalls_poison_and_exact_once_transactions,
    test_statistics_and_reset_priority,
    test_randomized_ownership,
  ]
  failed = 0
  for test in tests:
    try:
      test()
    except (AssertionError, AttributeError, TypeError, ValueError, RuntimeError) as error:
      failed += 1
      print(f"FAIL {test.__name__}: {type(error).__name__}: {error}", flush=True)
    else:
      print(f"PASS {test.__name__}", flush=True)
  print(f"Allocator integration tests: {len(tests) - failed} passed, {failed} failed", flush=True)
  return bool(failed)


if __name__ == "__main__":
  raise SystemExit(main())
