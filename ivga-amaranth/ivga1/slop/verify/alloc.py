"""Allocator properties, including bounded, end-to-end transaction scripts.

The heap below is an explicit reliable service model, NOT the production Heap:
accepted writes commit once, reads are read-first with two-edge latency, and
unqualified read data is unconstrained. The real registered-grant heap/control
arbiters and both allocator implementations are used. A stalled heap request is
accepted by the next edge (the only environmental liveness assumption).

Except for the zero-budget invariant, these are BMC checks from configuration
initialization, not unbounded proofs. Every script asserts a completion deadline
as well as covering completion. Free operands come only from saved successful
allocations; allocator results are asserted correct, never assumed correct.
"""

from amaranth import (
  Assert,
  Assume,
  Cat,
  Const,
  Cover,
  Elaboratable,
  Module,
  Mux,
  Signal,
)
from amaranth.asserts import AnyConst, AnySeq
from amaranth.hdl import Fragment
from amaranth.lib import wiring
from amaranth.lib.memory import Memory

from ivga0 import Addr, Word
from ivga0.mem.heap import HeapArbiter
from ivga0.mem.page_alloc import PAGE_SIZE, PageAlloc, PageAllocArbiter
from ivga0.mem.pair_alloc import (
  ALLOC_LANES,
  PAGE_NONE,
  PAIRS_PER_PAGE,
  PairAlloc,
  PairAllocArbiter,
)

from .core import FormalCase


class _Flat(Elaboratable):
  """Inline ordinary fragments before RTLIL; preserve SMT memory instances.

  Avoid deeply nested SMT function expansion in Z3 without changing core.py's
  preparation flow or mapping the memory into registers.
  """

  def __init__(self, module):
    self.module = module

  def elaborate(self, platform):
    result = Fragment()

    def inline(fragment, path):
      assert not fragment.domains
      for domain, statements in fragment.statements.items():
        result.add_statements(domain, statements)
      for child, name, src_loc in fragment.subfragments:
        child_path = f"{path}_{name}"
        if type(child) is Fragment:
          inline(child, child_path)
        else:
          result.add_subfragment(child, child_path, src_loc=src_loc)

    inline(Fragment.get(self.module, platform), "alloc")
    return result


def _heap(m, clients, *, n_pages, reserved=0, stalls=True):
  """Small SMT-array heap, with two application write ports for initialization."""
  arb = m.submodules.heap_arb = HeapArbiter(len(clients))
  for client, port in zip(arb.clients, clients):
    wiring.connect(m, port, client)
  real = arb.real
  memory = m.submodules.heap_model = Memory(shape=Word, depth=n_pages * PAGE_SIZE, init=[])
  # Explicit pipeline around an asynchronous SMT-array read. This preserves
  # read-first semantics without requiring memory_dff changes in the runner.
  read = memory.read_port(domain="comb")
  write = memory.write_port()
  app = [memory.write_port() for _ in range(2)]
  pending, response = Signal(), Signal()
  sampled, data = Signal(Word), Signal(Word)
  request = real.read | real.write
  phase = Signal()
  m.d.sync += phase.eq(~phase)
  # Directed alternating stalls avoid exponential schedule enumeration in the
  # longer scripts. Counts, free order, operands and application data remain
  # symbolic; these are not arbitrary-backpressure proofs.
  allow = phase if stalls else Const(1)
  denied = Signal()
  stalled = Signal()
  m.d.comb += [
    real.granted.eq(request & allow),
    read.addr.eq(real.read_addr),
    write.en.eq(real.write & allow),
    write.addr.eq(real.write_addr),
    write.data.eq(real.write_data),
    real.read_data.eq(Mux(response, data, AnySeq(Word.width))),
  ]
  m.d.sync += [pending.eq(real.read & allow), response.eq(pending), denied.eq(request & ~allow)]
  with m.If(real.read & allow):
    m.d.sync += sampled.eq(read.data)
  with m.If(pending):
    m.d.sync += data.eq(sampled)
  if stalls:
    # Bounded readiness, not an assumption about any allocator output/result.
    m.d.sync += Assume(~denied | allow)
    with m.If(request & ~allow):
      m.d.sync += stalled.eq(1)

  for port in clients:
    with m.If(port.read):
      m.d.sync += [Assert(port.read_addr >= reserved * PAGE_SIZE),
                   Assert(port.read_addr < n_pages * PAGE_SIZE)]
    with m.If(port.write):
      m.d.sync += [Assert(port.write_addr >= reserved * PAGE_SIZE),
                   Assert(port.write_addr < n_pages * PAGE_SIZE)]
    payload = Cat(port.read, port.write, port.read_addr, port.write_addr, port.write_data)
    previous = Signal(len(payload))
    was_denied = Signal()
    m.d.sync += [previous.eq(payload), was_denied.eq((port.read | port.write) & ~port.granted)]
    with m.If(was_denied):
      m.d.sync += Assert(payload == previous)

  for port in app:
    with m.If(port.en):
      m.d.sync += [Assert(port.addr >= reserved * PAGE_SIZE),
                   Assert(port.addr < n_pages * PAGE_SIZE), Assert(~request)]
  return app, stalled


def _deadline(m, finished, cycles):
  age = Signal(range(cycles + 1))
  with m.If(age < cycles):
    m.d.sync += age.eq(age + 1)
  with m.If(age == cycles):
    m.d.sync += Assert(finished, "allocator script must complete within its bound")
  m.d.sync += Cover(finished)


class _Script:
  """Emit states inside a caller-owned FSM; wait past done for bookkeeping."""

  def __init__(self, m, dut, *, ready, pairs=False, app=()):
    self.m, self.dut, self.pairs, self.app = m, dut, pairs, app
    self.ready = ready
    self.index = 0
    self.finished = Signal()
    outstanding = Signal()
    with m.If(dut.granted & ((dut.req_alloc != 0) | (dut.req_free != 0))):
      m.d.sync += [Assert(~outstanding), Assert(~dut.done), outstanding.eq(1)]
    with m.If(dut.done):
      m.d.sync += [Assert(outstanding), outstanding.eq(0)]
    # Symbolic legal application words; tag=1 excludes the FREED marker.
    self.values = [Cat(Const(1, 3), AnyConst(Word.width - 3)) for _ in range(2)]

  def operation(self, *, count=None, free=(), check=None, save=(), initialize=False):
    m, dut = self.m, self.dut
    i = self.index
    self.index += 1
    following = f"op{self.index}"
    free_field = dut.free_addrs if self.pairs else dut.free_ids
    result = dut.alloc_bases if self.pairs else dut.alloc_ids
    with m.State(f"op{i}"):
      m.d.comb += [dut.req_alloc.eq(0 if count is None else count), dut.req_free.eq(len(free))]
      for lane, addr in enumerate(free):
        m.d.comb += free_field[lane].eq(addr)
      with m.If(dut.granted):
        m.next = f"wait{i}"
    with m.State(f"wait{i}"):
      # Requests have been captured. Change all free operands while busy; the
      # command enables remain zero, so there is no accidental second request.
      m.d.comb += free_field.eq(AnySeq(len(free_field.as_value())))
      with m.If(dut.done):
        if check is not None:
          check()
        for lane, dest in enumerate(save):
          m.d.sync += dest.eq(result[lane])
        m.next = f"settle{i}"
    with m.State(f"settle{i}"), m.If(self.ready & ~dut.done):
      m.next = f"init{i}_0" if initialize else following
    if initialize:
      assert self.pairs and count is not None
      for lane in range(ALLOC_LANES):
        with m.State(f"init{i}_{lane}"):
          for half, port in enumerate(self.app):
            m.d.comb += [port.en.eq(lane < count), port.addr.eq(result[lane] ^ half),
                         port.data.eq(self.values[half])]
          m.next = following if lane == ALLOC_LANES - 1 else f"init{i}_{lane + 1}"

  def finish(self, check=None):
    m = self.m
    with m.State(f"op{self.index}"):
      m.d.comb += self.finished.eq(1)
      if check is not None:
        check()


def _page_case(n_pages, reserved):
  m = Module()
  pages = m.submodules.pages = PageAlloc(n_pages=n_pages, reserved_pages=reserved)
  arb = m.submodules.control_arb = PageAllocArbiter(1)
  wiring.connect(m, arb.real, pages.control)
  port = arb.clients[0]
  _, stalled = _heap(m, [pages.heap], n_pages=n_pages, reserved=reserved)
  remaining = n_pages - reserved
  saved = [Signal.like(pages.alloc_ids[0], name=f"issued_{i}") for i in range(remaining)]
  chosen = AnyConst(max(1, (remaining - 1).bit_length()))
  if remaining:
    m.d.sync += Assume(chosen < remaining)
  # The choice is an index into issued IDs, not an assumed-correct output ID.
  free_id = Const(0, len(pages.alloc_ids[0]))
  for i, page in enumerate(saved):
    free_id = Mux(chosen == i, page, free_id)

  def allocated(i):
    m.d.sync += [Assert(~port.oom), Assert(port.alloc_ids[0] == reserved + i),
                 Assert(pages.empty_id == reserved + i + 1), Assert(pages.free_count == 0),
                 Assert(pages.stat_allocs == i + 1), Assert(pages.stat_frees == 0)]
    for earlier in saved[:i]:
      m.d.sync += Assert(port.alloc_ids[0] != earlier)

  def oom():
    m.d.sync += [Assert(port.oom), Assert(pages.empty_id == n_pages),
                 Assert(pages.free_count == 0), Assert(pages.stat_allocs == remaining),
                 Assert(pages.stat_frees == 0)]

  script = _Script(m, port, ready=pages.granted)
  with m.FSM(init="op0"):
    for i in range(remaining):
      script.operation(count=1, save=[saved[i]], check=lambda i=i: allocated(i))
    script.operation(count=1, check=oom)
    if remaining:
      def freed():
        m.d.sync += [Assert(~port.oom), Assert(pages.empty_id == n_pages),
                     Assert(pages.free_count == 1), Assert(pages.free_head_id == free_id),
                     Assert(pages.stat_frees == 1)]

      def reused():
        m.d.sync += [Assert(~port.oom), Assert(port.alloc_ids[0] == free_id),
                     Assert(pages.empty_id == n_pages), Assert(pages.free_count == 0),
                     Assert(pages.stat_allocs == remaining + 1), Assert(pages.stat_frees == 1)]

      script.operation(free=[free_id], check=freed)
      script.operation(count=1, check=reused)
    script.finish()
  m.d.sync += [Assert(pages.empty_id >= reserved), Assert(pages.empty_id <= n_pages),
               Assert(pages.free_count <= pages.empty_id - reserved)]
  _deadline(m, script.finished, 45)
  if remaining:
    m.d.sync += Cover(script.finished & stalled)
    for i in range(remaining):
      m.d.sync += Cover(script.finished & (chosen == i))
  return _Flat(m)


def _zero_budget(n_pages):
  """Inductive safety of an actually exhausted reserved-prefix PageAlloc."""
  m = Module()
  pages = m.submodules.pages = PageAlloc(n_pages=n_pages, reserved_pages=n_pages)
  arb = m.submodules.control_arb = PageAllocArbiter(1)
  wiring.connect(m, arb.real, pages.control)
  port = arb.clients[0]
  # LANES=1, so every representable count is legal. No frees of unowned pages.
  m.d.comb += [port.req_alloc.eq(AnySeq(1)), port.req_free.eq(0),
               pages.heap.granted.eq(0), pages.heap.read_data.eq(AnySeq(Word.width)),
               Assert(pages.empty_id == n_pages), Assert(pages.free_count == 0),
               Assert(pages.stat_allocs == 0), Assert(pages.stat_frees == 0),
               Assert(~pages.heap.read & ~pages.heap.write),
               Assert(~port.done | port.oom), Cover(port.done & port.oom)]
  # Hold a selected request until its accepting edge, as required by the arbiter.
  previous = Signal()
  denied = Signal()
  m.d.sync += [previous.eq(port.req_alloc), denied.eq(port.req_alloc & ~port.granted)]
  with m.If(denied):
    m.d.sync += Assume(port.req_alloc == previous)
  return _Flat(m)


def _pair_system(*, reserved=0, stalls=True):
  m = Module()
  pages = m.submodules.pages = PageAlloc(n_pages=reserved + 1, reserved_pages=reserved)
  pairs = m.submodules.pairs = PairAlloc()
  page_arb = m.submodules.page_arb = PageAllocArbiter(1)
  pair_arb = m.submodules.pair_arb = PairAllocArbiter(1)
  wiring.connect(m, pairs.pages, page_arb.clients[0])
  wiring.connect(m, page_arb.real, pages.control)
  wiring.connect(m, pair_arb.real, pairs.control)
  app, stalled = _heap(m, [pages.heap, pairs.heap], n_pages=reserved + 1,
                       reserved=reserved, stalls=stalls)
  return m, pages, pairs, pair_arb.clients[0], app, stalled


def _pair_alloc_check(m, port, count, *, reserved=0, previous=()):
  m.d.sync += Assert(~port.oom)
  for lane in range(ALLOC_LANES):
    base = port.alloc_bases[lane]
    with m.If(lane < count):
      m.d.sync += [Assert(base >= reserved * PAGE_SIZE + 2),
                   Assert(base < (reserved + 1) * PAGE_SIZE), Assert(base[0] == 0)]
      for other in range(lane):
        m.d.sync += Assert(base != port.alloc_bases[other])
      for old in previous:
        m.d.sync += Assert(base != old)


def _pair_bump(reserved, *, single=False):
  m, pages, pairs, port, app, stalled = _pair_system(reserved=reserved)
  count = Const(1, len(port.req_alloc)) if single else AnyConst(len(port.req_alloc))
  m.d.sync += Assume((count >= 1) & (count <= ALLOC_LANES))
  script = _Script(m, port, ready=pairs.granted, pairs=True, app=app)

  def check():
    _pair_alloc_check(m, port, count, reserved=reserved)
    for lane in range(ALLOC_LANES):
      with m.If(lane < count):
        # Implementation fills lanes from the end; also proves bump uniqueness.
        m.d.sync += Assert(port.alloc_bases[lane] == reserved * PAGE_SIZE + (count - lane) * 2)
    m.d.sync += [Assert(pairs.pages_owned == 1), Assert(pairs.live_words == count * 2),
                 Assert(pairs.free_count == (PAIRS_PER_PAGE - count) * 2),
                 Assert(pairs.stat_allocs == count * 2), Assert(pairs.stat_frees == 0),
                 Assert(pairs.partial_head == reserved), Assert(pages.empty_id == reserved + 1)]

  with m.FSM(init="op0"):
    script.operation(count=count, check=check)
    script.finish()
  _deadline(m, script.finished, 32 if single else 68)
  for n in ([1] if single else range(1, ALLOC_LANES + 1)):
    m.d.sync += Cover(script.finished & stalled & (count == n))
  return _Flat(m)


def _pair_reuse(kind):
  """Allocate an anchor and victim, then test half/free-list/page reuse."""
  m, pages, pairs, port, app, stalled = _pair_system()
  script = _Script(m, port, ready=pairs.granted, pairs=True, app=app)
  a, anchor = [Signal(Addr, name=name) for name in ("victim", "anchor")]
  half = AnyConst(1)
  reclaim = kind == "reclaim"
  initial = 1 if reclaim else 2

  def initial_check():
    _pair_alloc_check(m, port, initial)
    m.d.sync += [Assert(pairs.live_words == initial * 2),
                 Assert(pairs.free_count == (PAIRS_PER_PAGE - initial) * 2)]

  def half_check():
    m.d.sync += [Assert(~port.oom), Assert(pairs.live_words == 3),
                 Assert(pairs.free_count == (PAIRS_PER_PAGE - 2) * 2),
                 Assert(pairs.pages_owned == 1), Assert(pairs.stat_frees == 1)]

  def fresh_check():
    _pair_alloc_check(m, port, 1, previous=[a, anchor])
    m.d.sync += [Assert(port.alloc_bases[0] == 6), Assert(pairs.live_words == 5),
                 Assert(pairs.free_count == (PAIRS_PER_PAGE - 3) * 2)]

  def freed_check():
    m.d.sync += [Assert(~port.oom), Assert(pairs.live_words == (0 if reclaim else 2)),
                 Assert(pairs.pages_owned == (0 if reclaim else 1)),
                 Assert(pairs.free_count == (0 if reclaim else (PAIRS_PER_PAGE - 1) * 2)),
                 Assert(pairs.stat_frees == 2)]

  def reuse_check():
    _pair_alloc_check(m, port, 1, previous=[] if reclaim else [anchor])
    m.d.sync += [Assert(port.alloc_bases[0] == a),
                 Assert(pairs.live_words == (2 if reclaim else 4)),
                 Assert(pairs.pages_owned == 1), Assert(pairs.stat_frees == 2),
                 Assert(pairs.free_count == (PAIRS_PER_PAGE - initial) * 2),
                 Assert(pages.stat_allocs == (2 if reclaim else 1)),
                 Assert(pages.stat_frees == (1 if reclaim else 0))]

  with m.FSM(init="op0"):
    script.operation(count=initial, check=initial_check,
                     save=[a] if reclaim else [a, anchor], initialize=True)
    if kind == "half":
      script.operation(free=[a ^ half], check=half_check)
      script.operation(count=1, check=fresh_check)
    else:
      # Both addresses are owned, distinct words of the saved successful pair.
      # Symbolic half chooses which word the reverse-lane free loop sees first.
      script.operation(free=[a ^ half, a ^ half ^ 1], check=freed_check)
      script.operation(count=1, check=reuse_check)
    script.finish()
  _deadline(m, script.finished, {"half": 80, "reuse": 104, "reclaim": 110}[kind])
  for h in range(2):
    m.d.sync += Cover(script.finished & stalled & (half == h))
  return _Flat(m)


def _pair_zero():
  m = Module()
  pages = m.submodules.pages = PageAlloc(n_pages=1, reserved_pages=1)
  pairs = m.submodules.pairs = PairAlloc()
  page_arb = m.submodules.page_arb = PageAllocArbiter(1)
  arb = m.submodules.pair_arb = PairAllocArbiter(1)
  wiring.connect(m, pairs.pages, page_arb.clients[0])
  wiring.connect(m, page_arb.real, pages.control)
  wiring.connect(m, arb.real, pairs.control)
  for heap in (pairs.heap, pages.heap):
    m.d.comb += [heap.granted.eq(0), heap.read_data.eq(AnySeq(Word.width)),
                 Assert(~heap.read & ~heap.write)]
  port = arb.clients[0]
  count = AnyConst(len(port.req_alloc))
  m.d.sync += Assume((count >= 1) & (count <= ALLOC_LANES))
  script = _Script(m, port, ready=pairs.granted, pairs=True)

  def oom():
    m.d.sync += [Assert(port.oom), Assert(port.alloc_bases.as_value() == 0),
                 Assert(pairs.pages_owned == 0), Assert(pairs.live_words == 0),
                 Assert(pairs.free_count == 0), Assert(pairs.partial_head == PAGE_NONE),
                 Assert(pairs.stat_allocs == 0), Assert(pairs.stat_frees == 0),
                 Assert(pages.empty_id == 1), Assert(pages.free_count == 0)]

  with m.FSM(init="op0"):
    # Repeating the failure also checks that OOM does not consume the budget.
    script.operation(count=count, check=oom)
    script.operation(count=count, check=oom)
    script.finish()
  _deadline(m, script.finished, 30)
  for n in range(1, ALLOC_LANES + 1):
    m.d.sync += Cover(script.finished & (count == n))
  return _Flat(m)


def _pair_rollback():
  """Real reachable prefix: 60 live pairs, reserve three, fail, undo, reuse.

  PAGE_SIZE is not reduced or monkeypatched. Filling a real 63-pair page needs
  substantially more than 200 edges; this deliberately deeper deterministic
  regression is separate from the small symbolic cases. No snapshot seeding or
  assumptions about allocator state are used to skip the reachable prefix.
  """
  m, pages, pairs, port, app, _ = _pair_system(stalls=False)
  script = _Script(m, port, ready=pairs.granted, pairs=True, app=app)

  def prefix(batch):
    _pair_alloc_check(m, port, 4)
    for lane in range(ALLOC_LANES):
      m.d.sync += Assert(port.alloc_bases[lane] == 2 + batch * 8 + (3 - lane) * 2)
    m.d.sync += [Assert(pairs.live_words == (batch + 1) * 8),
                 Assert(pairs.free_count == (PAIRS_PER_PAGE - (batch + 1) * 4) * 2)]

  def failed():
    m.d.sync += [Assert(port.oom), Assert(port.alloc_bases.as_value() == 0),
                 Assert(pairs.live_words == 120), Assert(pairs.pages_owned == 1),
                 Assert(pairs.free_count == 6), Assert(pairs.stat_allocs == 120),
                 Assert(pairs.stat_peak == 120), Assert(pairs.stat_frees == 0),
                 Assert(pairs.partial_head == 0),
                 Assert(pages.empty_id == 1), Assert(pages.free_count == 0),
                 Assert(pages.stat_allocs == 1), Assert(pages.stat_frees == 0)]

  def reused():
    _pair_alloc_check(m, port, 3)
    for lane in range(3):
      base = port.alloc_bases[lane]
      m.d.sync += Assert((base == 122) | (base == 124) | (base == 126))
    m.d.sync += [Assert(pairs.live_words == 126), Assert(pairs.free_count == 0),
                 Assert(pairs.stat_allocs == 126), Assert(pairs.stat_frees == 0),
                 Assert(pairs.partial_head == PAGE_NONE)]

  with m.FSM(init="op0"):
    for batch in range(15):
      script.operation(count=4, check=lambda batch=batch: prefix(batch))
    script.operation(count=4, check=failed)
    script.operation(count=3, check=reused)
    script.finish()
  _deadline(m, script.finished, 835)
  return _Flat(m)


def cases() -> list[FormalCase]:
  return [
    FormalCase(f"alloc_page_{n}_{reserved}",
               lambda n=n, reserved=reserved: _page_case(n, reserved),
               depth=47, mode="bmc")
    for n, reserved in ((1, 0), (2, 0), (2, 1))
  ] + [
    FormalCase(f"alloc_page_reserved_{n}", lambda n=n: _zero_budget(n), depth=12)
    for n in (1, 2)
  ] + [
    FormalCase(f"alloc_pair_bump_{reserved}", lambda reserved=reserved: _pair_bump(reserved),
               depth=70, mode="bmc", slow=True)
    for reserved in (0, 1)
  ] + [
    FormalCase(f"alloc_pair_{kind}", lambda kind=kind: _pair_reuse(kind),
               depth={"half": 82, "reuse": 106, "reclaim": 112}[kind], mode="bmc", slow=True)
    for kind in ("half", "reuse", "reclaim")
  ] + [
    FormalCase("alloc_pair_single", lambda: _pair_bump(0, single=True), depth=34, mode="bmc"),
    FormalCase("alloc_pair_reserved_oom", _pair_zero, depth=32, mode="bmc", slow=True),
    FormalCase("alloc_pair_rollback", _pair_rollback, depth=837, mode="bmc", slow=True),
  ]
