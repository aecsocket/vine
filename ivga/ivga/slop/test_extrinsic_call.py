"""Real-memory extrinsic call integration: python -B -m ivga.slop.test_extrinsic_call.

Only the supervisor and host request drivers are test scaffolding. In particular,
heap stalls suppress physical enables as well as grants; responses last one cycle.
No VCDs or generated files are produced.
"""

import unittest
from collections import Counter
from dataclasses import dataclass
from itertools import pairwise

from amaranth import Const, Module, Mux, Signal, Value
from amaranth.lib import wiring
from amaranth.sim import Simulator

from ivga import Port, Port2, Tag
from ivga.extrinsic import EXT_FNS, FN_ID_MASK, SWAP_BIT, TY_IDS
from ivga.extrinsic.branch import BRANCH
from ivga.extrinsic.n32 import N32
from ivga.mem.heap import CAPACITY, Heap, HeapArbiter
from ivga.mem.page_alloc import PAGE_SIZE, PageAlloc, PageAllocArbiter
from ivga.mem.pair_alloc import PAIRS_PER_PAGE, PairAlloc, PairAllocArbiter
from ivga.mem.stack import Stack, StackArbiter
from ivga.runtime.grafter import Graft, Instruction
from ivga.runtime.interact import Interact
from ivga.runtime.linker import Linker

PERIOD = 1e-6
LIMIT = 4096
POISON = 0xDEAD_BEEF_CAFE_F00D
FREED = Port.freed().as_value().value
GRANTS = ((True,), (False, True), (True, False, False, True, False))
FAULTS = ("oom", "active_full", "ext_copy", "ext_generic", "invalid_interaction")
IDS = {entry.name: i for i, entry in enumerate(EXT_FNS)}
ADD, SUB, EQ, NE, BR = (IDS[name] for name in
                      ("vi:n32:add", "vi:n32:sub", "vi:n32:eq", "vi:n32:ne", BRANCH))


def port(tag, payload=0, label=0):
  return Const({"tag": tag, "payload": payload, "label": label}, Port).as_value().value


def value(number, ty=N32):
  return port(Tag.EXT_VAL, number, TY_IDS[ty])


def wire(addr):
  return port(Tag.WIRE, addr)


def fields(bits):
  return bits & 7, (bits >> 3) & ((1 << 45) - 1), bits >> 48


def request(ctx, heap):
  read, write = ctx.get(heap.read), ctx.get(heap.write)
  return ((ctx.get(heap.read_addr) if read else None,
           ctx.get(heap.write_addr) if write else None,
           ctx.get(heap.write_data) if write else None) if read or write else None)


@dataclass(frozen=True)
class Transaction:
  owner: str
  read: int | None
  write: int | None
  data: int | None


class System:
  def __init__(self, n_pages=4):
    self.n_pages = n_pages
    self.m = m = Module()
    self.interact = m.submodules.interact = Interact()
    self.link = m.submodules.host_link = Linker()
    self.heap = m.submodules.heap = Heap()
    self.pages = m.submodules.pages = PageAlloc(n_pages)
    self.pairs = m.submodules.pairs = PairAlloc()
    self.stack = m.submodules.stack = Stack(Port2, depth=8)
    self.heap_arb = m.submodules.heap_arb = HeapArbiter(5)
    pair_arb = m.submodules.pair_arb = PairAllocArbiter(3)
    page_arb = m.submodules.page_arb = PageAllocArbiter(3)
    stack_arb = m.submodules.stack_arb = StackArbiter(3, Port2)
    heap_clients = (self.pages.heap, self.pairs.heap, self.interact.heap, self.link.heap)
    for client, real in zip(heap_clients, self.heap_arb.clients):
      wiring.connect(m, client, real)
    for arb, clients, real in (
      (pair_arb, (self.interact.pairs, self.link.alloc), self.pairs.control),
      (page_arb, (self.pairs.pages, self.interact.pages), self.pages.control),
      (stack_arb, (self.interact.active, self.link.active), self.stack.control),
    ):
      for i, client in enumerate(clients):
        wiring.connect(m, client, arb.clients[i])
      wiring.connect(m, arb.real, real)
    self.host_heap = self.heap_arb.clients[4]
    self.host_pairs, self.host_pages, self.host_stack = (
      pair_arb.clients[2], page_arb.clients[2], stack_arb.clients[2])

    self.allow, self.response = Signal(), Signal()
    real, heap = self.heap_arb.real, self.heap
    m.d.comb += [
      heap.read.eq(real.read & self.allow), heap.write.eq(real.write & self.allow),
      heap.read_addr.eq(real.read_addr), heap.write_addr.eq(real.write_addr),
      heap.write_data.eq(real.write_data), real.granted.eq(heap.granted & self.allow),
      real.read_data.eq(Mux(self.response, heap.read_data, POISON)),
    ]
    m.d.sync += self.response.eq(heap.read & heap.granted)

    self.clear_faults = Signal()
    self.latched = {name: Signal(name="latched_" + name) for name in FAULTS}
    sources = [self.interact, self.link, *self.interact.linkers]
    # Production fault outputs are combinational events, not sticky status bits.
    for name, latch in self.latched.items():
      event = Signal(name="event_" + name)
      m.d.comb += event.eq(sum(getattr(source.fault, name) for source in sources) != 0)
      with m.If(self.clear_faults):
        m.d.sync += latch.eq(0)
      with m.Else():
        m.d.sync += latch.eq(latch | event)
    for source in sources:
      m.d.comb += source.fault.halt.eq(self.latched["oom"] | self.latched["active_full"])
    self.sources = sources


class Harness:
  def __init__(self, ctx, system, grants):
    self.ctx, self.s, self.grants = ctx, system, grants
    self.cycle = self.stalls = self.poisoned = 0
    self.trace, self.denied = [], {}
    self.live, self.owned_pages, self.foreign_pages = set(), set(), set()
    ctx.set(system.allow, grants[0])
    self.pending_pairs = self.pending_pages = None
    self.allocs = self.frees = self.page_allocs = self.page_frees = 0
    self.free_words = Counter()
    self.done = self.pushes = self.pops = 0
    self.events = Counter()
    self.last_events = set()
    self.memory = [POISON ^ (addr * 0x10001) for addr in range(CAPACITY)]
    for addr, bits in enumerate(self.memory):
      ctx.set(system.heap.memory.data[addr], bits)

  async def tick(self):
    c, s = self.ctx, self.s
    accepted = []
    for i, hp in enumerate(s.heap_arb.clients):
      req = request(c, hp)
      if i in self.denied:
        assert req == self.denied[i], ("heap request changed under denial", i)
      self.denied.pop(i, None)
      if req is not None:
        if c.get(hp.granted):
          owner = ("pages", "pairs", "interact", "host_link", "host")[i]
          if i == 2:
            internal = [("call", s.interact.own_heap)] + [
              (f"link{j}", linker.heap) for j, linker in enumerate(s.interact.linkers)]
            owners = [name for name, p in internal if request(c, p) and c.get(p.granted)]
            assert len(owners) == 1, owners
            owner = owners[0]
          accepted.append(Transaction(owner, *req))
        else:
          self.denied[i] = req
          self.stalls += 1
    physical = request(c, s.heap)
    assert len(accepted) == int(physical is not None), "lost/duplicated physical transaction"
    expected_read = None
    if accepted:
      tx = accepted[0]
      assert (tx.read, tx.write, tx.data) == physical
      if tx.read is not None:
        expected_read = self.memory[tx.read]
      if tx.write is not None:
        self.memory[tx.write] = tx.data
      self.trace.append(tx)
    self.poisoned += int(not c.get(s.response))
    for kind, resource, pending_name in (
      ("pairs", s.pairs, "pending_pairs"), ("pages", s.pages, "pending_pages"),
    ):
      if c.get(resource.granted) and (c.get(resource.req_alloc) or c.get(resource.req_free)):
        assert getattr(self, pending_name) is None, "allocator accepted twice"
        count = c.get(resource.req_alloc)
        addrs = resource.free_addrs if kind == "pairs" else resource.free_ids
        frees = tuple(c.get(addrs[i]) for i in range(c.get(resource.req_free)))
        setattr(self, pending_name, (count, frees))
    events = {name for name in FAULTS if any(c.get(getattr(p.fault, name)) for p in s.sources)}
    self.events.update(events - self.last_events)
    self.last_events = events
    if c.get(s.stack.granted):
      self.pushes += c.get(s.stack.req_push)
      self.pops += c.get(s.stack.req_pop)
    await c.tick()
    self.cycle += 1
    assert c.get(s.heap_arb.real.read_data) == (POISON if expected_read is None else expected_read)
    for kind, resource, pending_name in (
      ("pairs", s.pairs, "pending_pairs"), ("pages", s.pages, "pending_pages"),
    ):
      if not c.get(resource.done):
        continue
      pending = getattr(self, pending_name)
      assert pending is not None, "allocator completion replayed"
      count, frees = pending
      setattr(self, pending_name, None)
      live = self.live if kind == "pairs" else self.owned_pages
      if count and not c.get(resource.oom):
        bases = resource.alloc_bases if kind == "pairs" else resource.alloc_ids
        added = {c.get(bases[i]) for i in range(count)}
        if kind == "pairs":
          added |= {base ^ 1 for base in tuple(added)}
        assert not added & live and len(added) == count * (2 if kind == "pairs" else 1)
        live.update(added)
        if kind == "pairs":
          self.allocs += len(added)
        else:
          self.page_allocs += len(added)
      if frees:
        assert len(set(frees)) == len(frees) and set(frees) <= live, ("double/foreign free", frees)
        live.difference_update(frees)
        if kind == "pairs":
          self.frees += len(frees)
          self.free_words.update(frees)
        else:
          self.page_frees += len(frees)
    self.done += c.get(s.interact.done)
    if c.get(s.interact.stat_reset):
      assert c.get(s.interact.stat_extrinsic) == 0, "stat_reset lost priority"
    c.set(s.allow, self.grants[self.cycle % len(self.grants)])

  async def idle(self, cycles=2):
    before = (self.done, self.pushes, self.pops, len(self.trace))
    for _ in range(cycles):
      await self.tick()
    assert before == (self.done, self.pushes, self.pops, len(self.trace)), "idle operation replay"
    self.account()

  def account(self):
    c, s = self.ctx, self.s
    assert self.pending_pairs is self.pending_pages is None
    assert c.get(s.pairs.live_words) == len(self.live)
    assert (c.get(s.pairs.stat_allocs), c.get(s.pairs.stat_frees)) == (self.allocs, self.frees)
    assert self.allocs - self.frees == len(self.live)
    stamps = Counter(tx.write for tx in self.trace if tx.owner == "pairs" and tx.data == FREED)
    assert stamps == self.free_words, "missing/replayed consumed-word free"
    assert (c.get(s.pages.stat_allocs), c.get(s.pages.stat_frees)) == (self.page_allocs, self.page_frees)
    assert c.get(s.pages.empty_id) - c.get(s.pages.free_count) == len(self.owned_pages)
    assert c.get(s.pairs.pages_owned) == len({addr // PAGE_SIZE for addr in self.live})
    assert c.get(s.pairs.stat_peak) >= len(self.live)

  async def transact(self, control, req, operands, *, reset=False):
    c = self.ctx
    for signal, bits in operands:
      c.set(Value.cast(signal), bits)
    c.set(req, 1)
    for _ in range(LIMIT):
      granted = c.get(control.granted)
      await self.tick()
      if granted:
        break
    else:
      raise AssertionError("host request never granted")
    c.set(req, 0)
    for signal, _ in operands:
      c.set(Value.cast(signal), POISON)
    for _ in range(LIMIT):
      if c.get(control.done):
        break
      await self.tick()
    else:
      raise AssertionError("request never completed")
    if reset:
      assert c.get(self.s.interact.stat_extrinsic) == 0

  async def write(self, addr, bits):
    hp, c = self.s.host_heap, self.ctx
    c.set(hp.write, 1)
    c.set(hp.write_addr, addr)
    c.set(hp.write_data, bits)
    for _ in range(LIMIT):
      granted = c.get(hp.granted)
      await self.tick()
      if granted:
        break
    else:
      raise AssertionError("host heap write timed out")
    c.set(hp.write, 0)
    c.set(hp.write_addr, 1023)
    c.set(hp.write_data, POISON)

  async def allocate(self):
    p, c = self.s.host_pairs, self.ctx
    await self.transact(p, p.req_alloc, [])
    assert not c.get(p.oom)
    bases = (c.get(p.alloc_bases[0]),)
    for base in bases:
      await self.write(base, 0)
      await self.write(base ^ 1, 0)
    await self.idle()
    return bases

  async def free(self, addresses):
    p = self.s.host_pairs
    for addr in addresses:
      await self.transact(p, p.req_free, [(p.free_addrs[0], addr)])
      await self.idle()

  async def page(self, page_id=None):
    p = self.s.host_pages
    if page_id is None:
      await self.transact(p, p.req_alloc, [])
      assert not self.ctx.get(p.oom)
      result = self.ctx.get(p.alloc_ids[0])
      self.foreign_pages.add(result)
    else:
      await self.transact(p, p.req_free, [(p.free_ids[0], page_id)])
      self.foreign_pages.remove(page_id)
      result = page_id
    await self.idle()
    return result

  async def interact(self, a, b, *, reset=False):
    i = self.s.interact
    before = self.done
    self.ctx.set(i.stat_reset, reset)
    await self.transact(i, i.req_interact, [(i.req_a, a), (i.req_b, b)], reset=reset)
    assert self.done == before + 1, "interaction must complete exactly once"
    self.ctx.set(i.stat_reset, 0)
    await self.idle()

  async def link(self, a, b):
    l = self.s.link
    await self.transact(l, l.req_link, [(l.req_a, a), (l.req_b, b)])
    await self.idle()

  async def pop(self):
    p, c = self.s.host_stack, self.ctx
    assert not c.get(p.empty)
    c.set(p.req_pop, 1)
    for _ in range(LIMIT):
      granted = c.get(p.granted)
      await self.tick()
      if granted:
        break
    else:
      raise AssertionError("worklist pop timed out")
    c.set(p.req_pop, 0)
    assert c.get(p.done)
    bits = c.get(p.pop_data.as_value())
    await self.idle()
    return bits & ((1 << 64) - 1), bits >> 64

  async def branch_table(self, *, cross_page=False):
    pages = [await self.page()]
    if cross_page:
      pages.append(await self.page())
      assert pages[1] == pages[0] + 1
    base = (pages[0] + 1) * PAGE_SIZE - 2 if cross_page else pages[0] * PAGE_SIZE + 4
    arms = []
    for i in range(3):
      template = pages[0] * PAGE_SIZE + 16 + 3 * i
      # Each arm is a valid, closed one-value graft, not an arbitrary pointer.
      await self.write(template, Const({"next_register": 1, "instructions_len": 1}, Graft).as_value().value)
      await self.write(template + 1, Const({"r0": 0}, Instruction).as_value().value)
      await self.write(template + 2, value(i))
      arms.append(port(Tag.GRAFT, template))
    await self.write(base, len(arms))
    for i, arm in enumerate(arms):
      await self.write(base + 1 + i, arm)
    return base, arms, pages

  async def fixture(self, fn, a, b, swapped=False, reverse=False):
    base, = await self.allocate()
    out, = await self.allocate()
    await self.write(base, a if swapped else b)
    await self.write(base ^ 1, wire(out))
    function = port(Tag.EXT_FN, base, fn | (SWAP_BIT if swapped else 0))
    pair = (function, b if swapped else a)
    return base, out, pair[::-1] if reverse else pair

  async def ready(self, fn, a, b, expected, swapped=False, reverse=False, *, reset=False):
    base, out, pair = await self.fixture(fn, a, b, swapped, reverse)
    allocs, frees = self.allocs, self.frees
    stat = self.ctx.get(self.s.interact.stat_extrinsic)
    await self.interact(*pair, reset=reset)
    assert self.memory[out] == expected, ("graph result", fn, swapped, reverse, hex(self.memory[out]), hex(expected))
    assert self.ctx.get(self.s.heap.memory.data[out]) == expected
    assert self.allocs == allocs and self.frees == frees + 2, "ready call must consume both function words"
    assert base not in self.live and (base ^ 1) not in self.live
    assert self.ctx.get(self.s.stack.empty)
    assert self.ctx.get(self.s.interact.stat_extrinsic) == (0 if reset else stat + 1)
    await self.free(sorted(self.live))
    self.empty()

  def empty(self):
    self.account()
    assert not self.live and self.owned_pages == self.foreign_pages
    assert self.ctx.get(self.s.pairs.pages_owned) == 0
    assert self.ctx.get(self.s.pairs.free_count) == 0
    assert self.ctx.get(self.s.stack.empty)

  def faults(self, expected=None):
    actual = {name for name, latch in self.s.latched.items() if self.ctx.get(latch)}
    assert actual == (set() if expected is None else {expected}), actual

  async def clear(self):
    self.ctx.set(self.s.clear_faults, 1)
    await self.tick()
    self.ctx.set(self.s.clear_faults, 0)
    await self.idle()
    self.faults()


def simulate(scenario, grants=GRANTS[2], n_pages=4):
  system = System(n_pages)
  sim = Simulator(system.m)
  sim.add_clock(PERIOD)

  async def bench(ctx):
    h = Harness(ctx, system, grants)
    await scenario(h)
    await h.idle(4)
    h.empty()
    assert not h.foreign_pages, "test leaked raw table pages"
    assert h.poisoned > 0
    if not all(grants):
      assert h.stalls > 0, "stall pattern did not exercise backpressure"

  sim.add_testbench(bench)
  sim.run()


class ExtrinsicCallTests(unittest.TestCase):
  def test_ready_registry_matrix(self):
    async def scenario(h):
      assert (SWAP_BIT, FN_ID_MASK) == (0x8000, 0x7FFF)
      assert len(EXT_FNS) == 5 and {ADD, SUB, EQ, NE, BR} == set(range(5))
      table, arms, pages = await h.branch_table(cross_page=True)
      # Raw table pages are deliberately not node-pair allocations.
      for swapped in (False, True):
        for reverse in (False, True):
          for fn, a, b, result in (
            (ADD, 0xFFFF_FFFF, 2, 1), (SUB, 7, 19, 0xFFFF_FFF4),
            (EQ, 23, 23, 1), (EQ, 23, 24, 0),
            (NE, 23, 23, 0), (NE, 23, 24, 1),
          ):
            await h.ready(fn, value(a), value(b), value(result), swapped, reverse)
          for index in (0, 1, 2):
            await h.ready(BR, value(index), value(table, BRANCH), arms[index], swapped, reverse)
      h.faults()
      assert h.ctx.get(h.s.interact.stat_extrinsic) == 36
      assert h.pushes == h.pops == 0
      for page in pages:
        await h.page(page)
      h.empty()

    for grants in GRANTS:
      with self.subTest(grants=grants):
        simulate(scenario, grants)

  def test_continuation_none_and_wire_chain(self):
    async def scenario(h):
      for chain_length in (0, 2):
        for swapped in (False, True):
          for reverse in (False, True):
            base, out, _ = await h.fixture(SUB, value(19), value(7), swapped, reverse)
            chain = [(await h.allocate())[0] for _ in range(chain_length)]
            anchors = [base, *chain]
            for left, right in pairwise(anchors):
              await h.write(left, wire(right))
            await h.write(anchors[-1], 0)
            function = port(Tag.EXT_FN, base, SUB | (SWAP_BIT if swapped else 0))
            current = value(7 if swapped else 19)
            pair = (current, function) if reverse else (function, current)
            start, allocs, frees = len(h.trace), h.allocs, h.frees
            stat, done = h.ctx.get(h.s.interact.stat_extrinsic), h.done
            await h.interact(*pair)
            assert h.done == done + 1, "deferral waited for the absent operand"
            assert h.ctx.get(h.s.interact.stat_extrinsic) == stat
            assert h.allocs == allocs + 2 and h.frees == frees + chain_length + 1
            assert h.ctx.get(h.s.stack.empty), "continuation ran before operand arrival"
            continuation = h.memory[anchors[-1]]
            tag, fresh, label = fields(continuation)
            assert tag == Tag.EXT_FN.value and label == (SUB | (SWAP_BIT if not swapped else 0))
            assert fresh not in {base, out, *chain} and fresh % 2 == 0
            assert h.memory[fresh] == current and h.memory[fresh ^ 1] == wire(out)
            trace = h.trace[start:]
            init = [i for i, tx in enumerate(trace) if tx.owner == "call" and tx.write in (fresh, fresh ^ 1)]
            assert len(init) == 2 and {trace[i].write for i in init} == {fresh, fresh ^ 1}
            assert all(trace[i].data == 0 for i in init)
            published = [i for i, tx in enumerate(trace) if tx.data == continuation and tx.write is not None]
            touched = [i for i, tx in enumerate(trace) if tx.owner.startswith("link")
                       and (tx.read in (fresh, fresh ^ 1) or tx.write in (fresh, fresh ^ 1))]
            assert published and touched and max(init) < min(published + touched), "continuation linked before initialization"
            await h.idle(5)
            before_push = h.pushes
            await h.link(wire(anchors[-1]), value(19 if swapped else 7))
            assert h.pushes == before_push + 1 and h.ctx.get(h.s.stack.top) == 1
            resumed = await h.pop()
            assert set(resumed) == {continuation, value(19 if swapped else 7)}
            assert h.ctx.get(h.s.stack.empty)
            await h.interact(*resumed)
            assert h.memory[out] == value(12), "resumed subtraction lost argument order"
            assert h.ctx.get(h.s.interact.stat_extrinsic) == stat + 1
            assert h.allocs == allocs + 2 and h.frees == frees + chain_length + 4
            await h.free(sorted(h.live))
            h.empty()
      assert h.pushes == h.pops == 8
      h.faults()

    for grants in (GRANTS[0], GRANTS[2]):
      with self.subTest(grants=grants):
        simulate(scenario, grants)

  def test_type_and_branch_bounds_faults_recover(self):
    async def scenario(h):
      table, arms, pages = await h.branch_table()
      cases = ((ADD, value(1, BRANCH), value(2)),
               (BR, value(table, BRANCH), value(0)),
               (BR, value(len(arms)), value(table, BRANCH)))
      for fn, a, b in cases:
        for swapped in (False, True):
          for reverse in (False, True):
            previous = h.events["ext_generic"]
            await h.ready(fn, a, b, port(Tag.ERASE), swapped, reverse)
            h.faults("ext_generic")
            assert h.events["ext_generic"] == previous + 1
            assert not h.ctx.get(h.s.interact.fault.ext_generic), "fault event incorrectly sticky"
            await h.idle(3)
            h.faults("ext_generic")
            await h.clear()
            await h.ready(SUB, value(9), value(4), value(5), swapped, reverse)
            h.faults()
      for page in pages:
        await h.page(page)
      h.empty()
    simulate(scenario)

  def test_invalid_function_id_and_recovery(self):
    async def scenario(h):
      for swapped in (False, True):
        for reverse in (False, True):
          base, out, pair = await h.fixture(FN_ID_MASK, value(5), value(6), swapped, reverse)
          stat, frees, allocs = h.ctx.get(h.s.interact.stat_extrinsic), h.frees, h.allocs
          await h.interact(*pair)
          h.faults("ext_generic")
          assert h.ctx.get(h.s.interact.stat_extrinsic) == stat
          assert h.allocs == allocs and h.frees == frees, "invalid ID consumed stored operands"
          assert h.ctx.get(h.s.stack.top) == 1
          cleanup = await h.pop()
          assert port(Tag.ERASE) in cleanup
          await h.interact(*cleanup)
          assert base not in h.live and (base ^ 1) not in h.live
          assert h.memory[out] == port(Tag.ERASE)
          await h.free(sorted(h.live))
          h.empty()
          await h.clear()
          await h.ready(ADD, value(41), value(1), value(42), swapped, reverse)
          h.faults()
      assert h.events["ext_generic"] == 4
    simulate(scenario)

  def test_deferred_allocation_oom_and_recovery(self):
    async def scenario(h):
      base, out, pair = await h.fixture(SUB, value(9), value(4))
      await h.write(base, 0)
      for _ in range(PAIRS_PER_PAGE - 2):
        await h.allocate()
      assert len(h.live) == PAIRS_PER_PAGE * 2
      before = (h.allocs, h.frees, set(h.live))
      start = len(h.trace)
      await h.interact(*pair)
      h.faults("oom")
      assert h.events["oom"] == 1 and h.ctx.get(h.s.interact.fault.halt)
      assert not h.ctx.get(h.s.interact.fault.oom)
      assert (h.allocs, h.frees, h.live) == before[:3]
      assert not any(tx.write is not None for tx in h.trace[start:]), "OOM modified the graph"
      assert h.memory[base] == 0 and h.memory[base ^ 1] == wire(out)
      assert h.ctx.get(h.s.interact.stat_extrinsic) == 0 and h.ctx.get(h.s.stack.empty)
      await h.free(sorted(h.live))
      h.empty()
      await h.clear()
      await h.ready(SUB, value(9), value(4), value(5), True, True)
      h.faults()
    simulate(scenario, n_pages=1)

  def test_statistics_reset_and_repeated_calls(self):
    async def scenario(h):
      for _ in range(3):
        await h.ready(ADD, value(2), value(3), value(5))
      assert h.ctx.get(h.s.interact.stat_extrinsic) == 3
      await h.ready(SUB, value(2), value(3), value(0xFFFF_FFFF), True, True, reset=True)
      assert h.ctx.get(h.s.interact.stat_extrinsic) == 0
      for i in range(4):
        await h.ready(NE, value(i), value(i + 1), value(1), bool(i & 1), bool(i & 2))
      assert h.ctx.get(h.s.interact.stat_extrinsic) == 4
      h.ctx.set(h.s.interact.stat_reset, 1)
      await h.idle()
      h.ctx.set(h.s.interact.stat_reset, 0)
      assert all(h.ctx.get(getattr(h.s.interact, "stat_" + name)) == 0
                 for name in ("annihilate", "copy", "commute", "graft", "extrinsic"))
      await h.ready(EQ, value(8), value(8), value(1))
      assert h.ctx.get(h.s.interact.stat_extrinsic) == 1
      h.faults()
    simulate(scenario)


if __name__ == "__main__":
  _ = unittest.main()
