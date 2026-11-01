"""Paired allocator contract tests.

From ivga/: python -m ivga.slop.test_alloc [--formal]

Allocation counts pairs (1..4) and returns even bases; free counts individual
words (1..2), processed in reverse input order. Only completely freed pairs are
reusable. Half-freed pairs strand capacity, even when many words are unowned.

The allocator DOES NOT initialize allocated cells. After checking completion,
outputs, ownership, and metadata, this harness explicitly initializes both words
as an application action. In particular, reused FREED markers must not survive
into the next lifetime. These application writes are not allocator transactions.

Heap requests commit exactly once on request && granted. A joint stamp writes
Port.freed() to the word and reads its mate, possibly at a different address.
Reads are read-first and respond immediately after the grant edge; the response
is consumed on the next edge, without a heap done signal. Busy inputs and data
outside response cycles are poisoned; denied requests must retain their payload.

Timing targets are deliberately STRICT, not fitted to the current implementation.
Acceptance is edge 1. Bump/OOM takes one edge. R pair pops use consecutive available
grant edges starting at acceptance, then one response-consumption edge; mixed
bump shares that final edge. Each free stamps/reads, then either accounts a
partial free on its response edge (overlapping the next stamp), or links a full
pair on the next available grant edge. A link occupies the write port, so the
next stamp follows it. Final partial completion is stamp-grant + one edge; final
full completion is the link-grant edge, with no done-only bubble. Grant patterns
are predetermined: DUT stalls never extend the ideal schedule. These bounds are
for this marker/list transaction protocol, not a comparison of architectures.
"""

import argparse
import gc
import random
import subprocess
import warnings
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from amaranth import Module
from amaranth.hdl import UnusedElaboratable
from amaranth.sim import Simulator

from ivga import ADDR_MAX, Port
from ivga.mem.alloc import ADDR_EMPTY, ALLOC_LANES, FREE_LANES, Alloc, AllocArbiter

EMPTY = ADDR_EMPTY.value
FREED = Port.freed().as_value().value
POISON = 0xDEAD_BEEF_CAFE_F00D
PERIOD = 1e-6
GRANTS = ((True,), (False, True), (True, False), (False, False, True, True, False))


def words(bases):
  return tuple(addr for base in bases for addr in (base, base ^ 1))


def link_word(addr):
  # Independent encoding: NONE tag in bits 0..2, address in payload, label zero.
  assert 0 <= addr <= EMPTY
  return addr << 3


def link_addr(word):
  assert word & 0b111 == 0 and 0 <= word >> 3 <= EMPTY, ("malformed free-list port", word)
  return word >> 3


@dataclass(frozen=True)
class Transaction:
  kind: str
  read_addr: int | None = None
  write_addr: int | None = None
  data: int | None = None
  full: bool = False
  word: int = 0

  @property
  def payload(self):
    return self.kind, self.read_addr, self.write_addr, self.data


@dataclass
class Oracle:
  """Word ownership is independent of the whole-pair LIFO stack."""

  capacity: int
  bump: int = 0
  free: list[int] = field(default_factory=list)
  live: set[int] = field(default_factory=set)
  released: set[int] = field(default_factory=set)

  @property
  def head(self):
    return self.free[-1] if self.free else EMPTY

  def allocate(self, count):
    assert 0 < count <= ALLOC_LANES
    if 2 * count > self.capacity - self.bump + 2 * len(self.free):
      return True, (), []
    result, transactions = [0] * count, []
    reused = min(count, len(self.free))
    for lane in range(count - 1, count - reused - 1, -1):
      base = self.free.pop()
      result[lane] = base
      transactions.append(Transaction("pop", read_addr=base))
      self.released.difference_update((base, base ^ 1))
    remaining = count - reused
    result[:remaining] = range(self.bump, self.bump + 2 * remaining, 2)
    self.bump += 2 * remaining
    allocated = set(words(result))
    assert len(allocated) == 2 * count and not allocated & self.live
    self.live.update(allocated)
    return False, tuple(result), transactions

  def release(self, addresses):
    assert 0 < len(addresses) <= FREE_LANES
    assert len(set(addresses)) == len(addresses)
    assert set(addresses) <= self.live, "test attempted an invalid/double free"
    transactions = []
    for index, addr in enumerate(reversed(addresses)):
      self.live.remove(addr)
      full = (addr ^ 1) in self.released
      transactions.append(Transaction("stamp", addr ^ 1, addr, FREED, full, index))
      self.released.add(addr)
      if full:
        base = addr & ~1
        transactions.append(Transaction("link", write_addr=base, data=link_word(self.head), word=index))
        self.free.append(base)
    return transactions


@dataclass
class Response:
  value: int
  due: int
  transaction: Transaction


class HeapModel:
  """Exact-once payload/order checks independent of the timing oracle."""

  def __init__(self, capacity, grants=(True,)):
    assert any(grants)
    self.grants = grants
    self.words = [POISON ^ (i * 0x10001) for i in range(capacity)]
    self.pending = deque()
    self.expected = deque()
    self.accepted = []
    self.accepted_cycles = []
    self.responded = 0
    self.stalled = None
    self.stall_cycles = 0
    self.stamps = {}

  def drive_data(self, ctx, port, cycle):
    response = self.pending[0] if self.pending and self.pending[0].due == cycle else None
    ctx.set(port.read_data.as_value(), response.value if response else POISON)
    return response

  def drive(self, ctx, port, cycle):
    request = ctx.get(port.read) or ctx.get(port.write)
    ctx.set(port.granted, bool(request and self.grants[cycle % len(self.grants)]))
    return self.drive_data(ctx, port, cycle)

  def accept(self, ctx, port, cycle, response):
    read, write = ctx.get(port.read), ctx.get(port.write)
    request = None
    if read or write:
      read_addr = ctx.get(port.read_addr) if read else None
      write_addr = ctx.get(port.write_addr) if write else None
      for addr in (read_addr, write_addr):
        assert addr is None or 0 <= addr < len(self.words), ("heap address", addr)
      kind = "stamp" if read and write else "pop" if read else "link"
      request = (kind, read_addr, write_addr, ctx.get(port.write_data.as_value()) if write else None)
    if self.stalled is not None:
      assert request == self.stalled, ("payload changed/withdrawn before grant", self.stalled, request)
    granted = bool(ctx.get(port.granted))
    self.stalled = request if request and not granted else None
    self.stall_cycles += int(self.stalled is not None)
    if response:
      assert self.pending.popleft() is response
      self.responded += 1
    if not request or not granted:
      return None
    assert self.expected, ("unexpected/reissued heap transaction", request, cycle)
    expected = self.expected.popleft()
    assert request == expected.payload, ("wrong heap transaction", expected.payload, request, cycle)
    if read:
      assert not self.pending, "multiple outstanding allocator reads"
      # Sample before the write, including if a future request uses equal addresses.
      value = self.words[expected.read_addr]
      self.pending.append(Response(value, cycle + 1, expected))
      if expected.kind == "stamp":
        assert (value == FREED) == expected.full, ("mate marker/ownership", expected, value)
        self.stamps[expected.word] = cycle
    if expected.kind == "link":
      assert cycle >= self.stamps[expected.word] + 1, "link before mate response"
    if write:
      self.words[expected.write_addr] = expected.data
    self.accepted.append(expected)
    self.accepted_cycles.append(cycle)
    return expected


def minimum_schedule(start, transactions, grants):
  """Absolute grant cycles and post-edge completion, using ONLY planned traffic.

  A partial response overlaps the next stamp. A full response may link at that
  edge, but the next stamp must wait until the following edge. Thus each planned
  transaction can use the next available grant; only a final read needs an extra
  response edge, which does not itself require a grant.
  """
  assert any(grants)
  cycle, accepted = start, []
  for _ in transactions:
    while not grants[cycle % len(grants)]:
      cycle += 1
    accepted.append(cycle)
    cycle += 1
  done = cycle + int(transactions[-1].read_addr is not None) if transactions else start + 1
  return tuple(accepted), done


class TimingFailure(AssertionError):
  pass


class Harness:
  def __init__(self, ctx, dut, *, grants=(True,), reset_port=None):
    assert (ALLOC_LANES, FREE_LANES) == (4, 2)
    assert (len(dut.req_alloc), len(dut.req_free)) == (3, 2)
    self.ctx, self.dut = ctx, dut
    self.reset_port = dut.stat_reset if reset_port is None else reset_port
    self.oracle = Oracle(dut.capacity)
    self.heap = HeapModel(dut.capacity, grants)
    self.cycle = 0
    self.stats = (0, 0, 0)
    self.kind = None
    self.live_data = {}
    self.lifetime = 0
    self.busy_cycles = 0
    self.timing_checks = 0
    self.timing_failures = []
    self.check_state()

  def state(self):
    return tuple(self.ctx.get(signal) for signal in (self.dut.empty, self.dut.free_count, self.dut.free_head))

  def check_state(self):
    bump, count, head = self.state()
    assert 0 <= bump <= self.dut.capacity and bump % 2 == 0, ("bump", bump)
    assert 0 <= count <= bump and count % 2 == 0, ("free_count", count)
    assert (head == EMPTY) == (count == 0), ("head/count mismatch", head, count)
    assert head == EMPTY or (0 <= head < bump and head % 2 == 0), ("head", head)

  def check_memory(self, skip=()):
    oracle, memory = self.oracle, self.heap.words
    assert oracle.live.isdisjoint(oracle.released)
    assert oracle.live | oracle.released == set(range(oracle.bump))
    assert len(set(oracle.free)) == len(oracle.free)
    head = oracle.head
    for base in reversed(oracle.free):
      assert head == base and base % 2 == 0
      assert {base, base ^ 1} <= oracle.released
      assert memory[base ^ 1] == FREED, ("odd pair marker", base, memory[base ^ 1])
      head = link_addr(memory[base])
    assert head == EMPTY, ("free-list terminal pointer", head)
    complete = set(words(oracle.free))
    for addr in oracle.released - complete:
      assert memory[addr] == FREED, ("partial free marker", addr, memory[addr])
    for addr in oracle.live - set(skip):
      assert memory[addr] == self.live_data[addr], ("live word overwritten", addr)

  async def tick(self, reset: bool | Literal["read", "link"] = False):
    ctx, dut = self.ctx, self.dut
    response = self.heap.drive(ctx, dut.heap, self.cycle)
    if reset == "read":
      reset = response is not None
    elif reset == "link":
      reset = bool(ctx.get(dut.heap.write) and not ctx.get(dut.heap.read) and ctx.get(dut.heap.granted))
    ctx.set(self.reset_port, bool(reset))
    before = self.state()
    accepted = self.heap.accept(ctx, dut.heap, self.cycle, response)
    pop = response is not None and response.transaction.kind == "pop"
    link = accepted is not None and accepted.kind == "link"
    if self.kind == "free":
      if accepted is not None and accepted.kind == "stamp":
        self.stamped |= 1 << accepted.word
      if response is not None and not response.transaction.full:
        self.account_due |= 1 << response.transaction.word
      if accepted is not None and link:
        self.account_due |= 1 << accepted.word
    await ctx.tick()
    self.cycle += 1
    _ = self.heap.drive_data(ctx, dut.heap, self.cycle)
    after = self.state()
    self.check_state()
    assert after[1] == before[1] + 2 * link - 2 * pop, ("free_count metadata/response edge", before, after)
    expected_head = before[2]
    if accepted is not None and link:
      expected_head = accepted.write_addr
      assert accepted.data == link_word(before[2]), "link payload must contain the old free head"
    elif response is not None and pop:
      expected_head = link_addr(response.value)
    assert after[2] == expected_head, ("free_head metadata/response edge", after[2], expected_head)
    bump = after[0] - before[0]
    assert bump >= 0 and bump % 2 == 0
    if self.kind == "free":
      assert bump == 0 and not pop
    else:
      assert accepted is None or accepted.kind == "pop", "allocation/idle wrote metadata"
    allocs, frees, peak = self.stats
    actual = tuple(ctx.get(signal) for signal in (dut.stat_allocs, dut.stat_frees, dut.stat_peak))
    expected_allocs = 0 if reset else allocs + bump + 2 * pop
    expected_peak = 0 if reset else max(peak, before[0])
    assert (actual[0], actual[2]) == (expected_allocs, expected_peak), ("alloc statistics/reset/peak lag", self.cycle, actual)
    if self.kind == "free":
      # Accounting may commit with the stamp or later bookkeeping, but exactly
      # once per word, no earlier than its stamp and no later than its response
      # (partial) or link grant (full). Enumerate the <=4 possibilities so reset
      # can win even when it coincides with an otherwise invisible increment.
      options = set()
      for value, accounted in self.free_options:
        for newly in range(1 << self.timing_count):
          if newly & (accounted | ~self.stamped):
            continue
          mask = accounted | newly
          if mask & self.account_due != self.account_due:
            continue
          total = 0 if reset else value + newly.bit_count()
          if total == actual[1]:
            options.add((total, mask))
      assert options, ("free statistics/exact-once/reset", self.cycle, actual[1], self.free_options)
      self.free_options = options
    else:
      assert actual[1] == (0 if reset else frees), ("unexpected free statistics", actual)
    self.stats = actual
    if ctx.get(dut.done):
      assert not self.heap.pending, "done before consuming accepted heap read"
      assert not self.heap.expected, "done before all metadata transactions accepted"
    if not ctx.get(dut.granted):
      self.busy_cycles += 1
    return bool(ctx.get(dut.done))

  def begin(self, kind, count=0, addresses=()):
    assert not self.heap.pending and not self.heap.expected and self.heap.stalled is None
    assert self.kind is None
    self.kind = kind
    if kind == "alloc":
      self.oom, self.result, transactions = self.oracle.allocate(count)
    else:
      self.oom, self.result = False, ()
      transactions = self.oracle.release(addresses)
    self.heap.expected.extend(transactions)
    self.heap.stamps.clear()
    self.timing_start = self.cycle
    self.transaction_start = len(self.heap.accepted_cycles)
    self.minimum_grants, self.minimum_done = minimum_schedule(self.cycle, transactions, self.heap.grants)
    self.timing_path = ("OOM" if self.oom else "free" if kind == "free" else
                        "bump" if not transactions else "reuse" if len(transactions) == count else "mixed reuse/bump")
    self.timing_count = count if kind == "alloc" else len(addresses)
    self.stamped = self.account_due = 0
    self.free_options = {(self.stats[1], 0)}

  def finish(self, count=0):
    ctx, dut, oracle = self.ctx, self.dut, self.oracle
    assert ctx.get(dut.done)
    assert bool(ctx.get(dut.oom)) == self.oom, ("oom", self.oom)
    assert self.state() == (oracle.bump, 2 * len(oracle.free), oracle.head), ("oracle", self.state(), oracle)
    result = ()
    if self.kind == "alloc" and not self.oom:
      result = tuple(ctx.get(dut.alloc_bases[i]) for i in range(count))
      assert all(base % 2 == 0 for base in result), ("odd pair base", result)
      assert len(set(words(result))) == 2 * count, ("overlapping allocation", result)
      assert result == self.result, ("ownership/free-first lane order", result, self.result)
    elif self.kind == "free":
      assert all(mask == (1 << self.timing_count) - 1 for _, mask in self.free_options), "unaccounted free at done"
    self.check_memory(skip=words(result))
    actual_grants = tuple(self.heap.accepted_cycles[self.transaction_start:])
    self.timing_checks += 1
    if actual_grants != self.minimum_grants or self.cycle != self.minimum_done or not ctx.get(dut.granted):
      actual_edges = tuple(cycle - self.timing_start + 1 for cycle in actual_grants)
      minimum_edges = tuple(cycle - self.timing_start + 1 for cycle in self.minimum_grants)
      self.timing_failures.append((self.timing_path, (
        f"{self.timing_path}({self.timing_count}): done after {self.cycle - self.timing_start} edges "
        f"(minimum {self.minimum_done - self.timing_start}); "
        f"heap grant edges {actual_edges} (expected {minimum_edges}); "
        f"ready at done={bool(ctx.get(dut.granted))}"
      )))
    self.kind = None
    # Application initialization, AFTER all completion/metadata checks. Neither
    # fresh allocation nor reuse promises to clear memory; never model it as such.
    if result:
      self.lifetime += 1
      for addr in words(result):
        port = Port.none() if (addr + self.lifetime) % 3 == 0 else Port.comb(addr, self.lifetime)
        value = port.as_value().value
        assert value != FREED
        self.heap.words[addr] = self.live_data[addr] = value
      self.check_memory()
    return result

  def assert_minimum_timing(self):
    examples = {}
    for path, message in self.timing_failures:
      examples.setdefault(path, message)
    if self.timing_failures:
      raise TimingFailure(
        f"nonminimal cycle timing in {len(self.timing_failures)}/{self.timing_checks} operations:\n  "
        + "\n  ".join(examples.values())
      )

  async def operation(self, kind, count=0, addresses=(), *, poison=True, reset=False, simultaneous=(), control=None):
    ctx, dut = self.ctx, self.dut
    control = dut if control is None else control
    assert ctx.get(dut.granted), "allocator must be ready when idle"
    self.begin(kind, count, addresses)
    ctx.set(control.req_alloc, count if kind == "alloc" else 0)
    frees = simultaneous if kind == "alloc" else addresses
    ctx.set(control.req_free, len(frees))
    ctx.set(control.free_addrs, (list(frees) + [EMPTY] * FREE_LANES)[:FREE_LANES])
    assert ctx.get(control.granted), "requesting client must be granted when uncontested"
    completed = await self.tick(reset=reset)
    if self.oom:
      assert completed, "OOM must complete atomically on acceptance"
    for _ in range(2048):
      if completed:
        break
      assert not ctx.get(dut.granted), "busy allocator accepted a new control request"
      ctx.set(control.req_alloc, 7 if poison else 0)
      ctx.set(control.req_free, 3 if poison else 0)
      ctx.set(control.free_addrs, [EMPTY] * FREE_LANES)
      completed = await self.tick(reset=reset)
    else:
      raise AssertionError("allocator timed out (2048 edges)")
    ctx.set(control.req_alloc, 0)
    ctx.set(control.req_free, 0)
    ctx.set(self.reset_port, 0)
    return self.finish(count)

  async def alloc(self, count, **kwargs):
    return await self.operation("alloc", count=count, **kwargs)

  async def free(self, addresses, **kwargs):
    return await self.operation("free", addresses=tuple(addresses), **kwargs)

  async def idle(self, cycles=1, *, reset=False):
    self.ctx.set(self.dut.req_alloc, 0)
    self.ctx.set(self.dut.req_free, 0)
    for _ in range(cycles):
      assert not await self.tick(reset=reset), "done must be a pulse, not a level"
      assert not self.ctx.get(self.dut.oom)
      assert self.ctx.get(self.dut.granted)
      assert not self.ctx.get(self.dut.heap.read) and not self.ctx.get(self.dut.heap.write)
      self.check_memory()


def simulate(capacity, scenario, *, grants=(True,)):
  dut = Alloc(capacity)
  sim = Simulator(dut)
  sim.add_clock(PERIOD)

  async def bench(ctx):
    h = Harness(ctx, dut, grants=grants)
    assert ctx.get(dut.granted) and not ctx.get(dut.done) and not ctx.get(dut.oom)
    await scenario(h)
    await h.idle(2)
    assert not h.heap.pending and not h.heap.expected and h.heap.stalled is None
    assert sum(tx.read_addr is not None for tx in h.heap.accepted) == h.heap.responded
    h.assert_minimum_timing()

  sim.add_testbench(bench)
  sim.run()


def run_cases(cases):
  """Independent simulators keep one failing assertion case from hiding others."""
  failures = []
  for label, case in cases:
    try:
      case()
    except AssertionError as error:
      failures.append((label, error))
  if failures:
    message = "\n".join(f"{label}: {error}" for label, error in failures)
    error_type = TimingFailure if all(isinstance(error, TimingFailure) for _, error in failures) else AssertionError
    raise error_type(message)


def test_minimum_schedule():
  stamp = Transaction("stamp", 1, 0, FREED)
  link = Transaction("link", write_addr=0, data=link_word(EMPTY))
  pop = Transaction("pop", read_addr=0)
  assert minimum_schedule(4, [], (False, True)) == ((), 5)
  assert minimum_schedule(0, [stamp], (True,)) == ((0,), 2)
  assert minimum_schedule(0, [stamp, stamp], (True,)) == ((0, 1), 3)
  assert minimum_schedule(0, [stamp, link], (True,)) == ((0, 1), 2)
  assert minimum_schedule(0, [stamp, link, stamp], (True,)) == ((0, 1, 2), 4)
  assert minimum_schedule(0, [stamp, stamp, link], (True,)) == ((0, 1, 2), 3)
  assert minimum_schedule(0, [stamp, link, stamp, link], (True,)) == ((0, 1, 2, 3), 4)
  grants = (False, True, False, False, True)
  assert minimum_schedule(0, [pop] * 3, grants) == ((1, 4, 6), 8)
  assert minimum_schedule(2, [pop] * 2, grants) == ((4, 6), 8)
  assert minimum_schedule(0, [stamp, stamp, link], grants) == ((1, 4, 6), 7)
  # A final response needs no further grant, whether it is a pop or a partial free.
  for read in (pop, stamp):
    assert minimum_schedule(1, [read], (False, True, False)) == ((1,), 3)


def test_minimum_cycle_timing():
  async def scenario(h):
    for count in range(1, ALLOC_LANES + 1):
      bases = await h.alloc(count)
      for base in bases:
        await h.free((base, base ^ 1))
      reused = await h.alloc(count)
      if count > 1:
        for base in reused[:-1]:
          await h.free((base ^ 1, base))
        await h.alloc(count)
  run_cases((str(grants), lambda grants=grants: simulate(64, scenario, grants=grants)) for grants in GRANTS)


def test_invalid_request_assertions():
  def expect_assert(req_alloc, req_free, message, *, capacity=16, setup=None, grants=(True,)):
    dut = Alloc(capacity)
    sim = Simulator(dut)
    sim.add_clock(PERIOD)

    async def bench(ctx):
      h = Harness(ctx, dut, grants=grants)
      if setup is not None:
        await setup(h)
      await h.idle()
      ctx.set(dut.req_alloc, req_alloc)
      ctx.set(dut.req_free, req_free)
      ctx.set(dut.free_addrs, [EMPTY] * FREE_LANES)
      assert ctx.get(dut.granted) and not ctx.get(dut.done) and not ctx.get(dut.oom)
      ctx.set(dut.heap.granted, 0)
      await ctx.tick()

    sim.add_testbench(bench)
    try:
      sim.run()
    except AssertionError as error:
      assert message in str(error), ("wrong hardware assertion", req_alloc, req_free, str(error))
    else:
      raise AssertionError(f"expected hardware assertion: {message} ({req_alloc=}, {req_free=})")

  async def full(h):
    await h.alloc(1)

  async def mixed(h):
    await h.alloc(4)
    await h.free((5, 4))
    await h.free((2,))

  alloc_message = "Allocation request exceeds lane count"
  free_message = "Free request exceeds lane count"
  cases = []
  for count in (5, 6, 7):
    for grants in GRANTS:
      cases.append((f"alloc {count}, {grants}", lambda count=count, grants=grants:
                    expect_assert(count, 0, alloc_message, grants=grants)))
    cases.append((f"alloc {count}, mixed heap", lambda count=count:
                  expect_assert(count, 0, alloc_message, setup=mixed, grants=(True, False, False, True))))
  cases.append(("free 3", lambda: expect_assert(0, 3, free_message, capacity=2, setup=full)))
  for free_count in (1, 3):
    cases.append((f"allocation priority over free {free_count}", lambda free_count=free_count:
                  expect_assert(5, free_count, alloc_message, capacity=2, setup=full, grants=(False, True))))
  run_cases(cases)


def test_bump_and_atomic_oom():
  async def scenario(h):
    await h.idle(3)
    assert await h.alloc(1) == (0,)
    assert h.stats == (2, 0, 0), "peak must lag the first word bump by one edge"
    assert await h.alloc(3) == (2, 4, 6)
    assert await h.alloc(4) == (8, 10, 12, 14)
    await h.alloc(3)
    assert h.oracle.bump == 16
    assert await h.alloc(2) == (16, 18)
    for _ in range(3):
      await h.alloc(1)
    assert h.stats[:2] == (20, 0) and not h.heap.accepted
  simulate(20, scenario)


def test_individual_word_reuse():
  async def scenario(h):
    await h.alloc(2)
    await h.free((1,))
    assert not h.oracle.free and h.oracle.released == {1}
    assert h.heap.accepted == [Transaction("stamp", 0, 1, FREED)]
    await h.free((2,))
    assert not h.oracle.free and h.oracle.released == {1, 2}
    before = len(h.heap.accepted)
    await h.alloc(1)  # Two unrelated halves cannot form a reusable pair.
    assert h.oom and len(h.heap.accepted) == before
    await h.free((0,))
    assert h.oracle.free == [0] and h.heap.words[0] == link_word(EMPTY)
    assert await h.alloc(1) == (0,)
    assert h.heap.words[0] != FREED and h.heap.words[1] != FREED
    await h.free((3,))
    assert h.oracle.free == [2]
    assert await h.alloc(1) == (2,)
    assert h.oracle.live == set(range(4)) and h.stats[:2] == (8, 4)
  simulate(4, scenario, grants=(False, False, True))


def test_mixed_reuse_and_atomic_oom():
  async def scenario(h):
    await h.alloc(4)
    await h.free((5, 4))
    await h.free((2, 3))
    assert h.oracle.free == [4, 2]
    before = len(h.heap.accepted)
    await h.alloc(4)  # Only three pairs available: do not pop either pair.
    assert h.oom and h.oracle.free == [4, 2]
    assert len(h.heap.accepted) == before
    assert await h.alloc(3) == (8, 4, 2)
    assert len(h.heap.accepted) - before == 2
    await h.free((0, 6))
    await h.alloc(1)
    assert h.oom and not h.oracle.free
    await h.free((7, 1))  # Both words complete different pairs, in reverse order.
    assert h.oracle.free == [0, 6]
    assert await h.alloc(2) == (0, 6)
  simulate(10, scenario, grants=(True, False, False, True))


def test_eight_word_batches_and_poisoned_inputs():
  async def scenario(h):
    for _ in range(3):
      await h.alloc(4)
    for parity in (0, 1):
      addresses = list(range(parity, 24, 2))
      for index in range(0, len(addresses), FREE_LANES):
        await h.free(addresses[index:index + FREE_LANES])
      if parity == 0:
        assert not h.oracle.free and len(h.oracle.released) == 12
        await h.alloc(1)
        assert h.oom
    assert h.stats[:2] == (24, 24) and len(h.oracle.free) == 12
    for _ in range(3):
      await h.alloc(4)
    assert h.stats[:2] == (48, 24)
    assert sum(tx.kind == "pop" for tx in h.heap.accepted) == 12
    assert sum(tx.kind == "stamp" for tx in h.heap.accepted) == 24
    assert sum(tx.kind == "link" for tx in h.heap.accepted) == 12
    assert h.heap.stall_cycles and h.busy_cycles
  simulate(24, scenario, grants=(False, False, True, True, False))


def test_free_acceptance_edges():
  async def scenario(h):
    await h.alloc(4)
    await h.free((0, 2))  # Consecutive partials must overlap bookkeeping/stamp.
    assert h.heap.accepted == [Transaction("stamp", 3, 2, FREED), Transaction("stamp", 1, 0, FREED, word=1)]
    await h.free((1, 3))  # Consecutive full pairs require a link between stamps.
    assert [tx.payload for tx in h.heap.accepted[2:]] == [
      ("stamp", 2, 3, FREED), ("link", None, 2, link_word(EMPTY)),
      ("stamp", 0, 1, FREED), ("link", None, 0, link_word(2)),
    ]
    await h.free((4, 5))  # Same pair, odd half stamped first.
    await h.free((7, 6))  # Same pair, even half stamped first.
    assert h.oracle.free == [2, 0, 4, 6]
    assert await h.alloc(4) == (2, 0, 4, 6)
    assert h.stats[:2] == (16, 8)
  run_cases((str(grants), lambda grants=grants: simulate(8, scenario, grants=grants)) for grants in GRANTS)


def test_allocation_priority():
  async def scenario(h):
    await h.alloc(2, simultaneous=(EMPTY,) * 3)  # Ignored oversized free encoding.
    await h.alloc(1, simultaneous=(0, 1))
    await h.alloc(3, simultaneous=(2, 3))
    assert h.oracle.live == {0, 1, 2, 3} and h.stats[:2] == (4, 0)
    await h.free((0, 1))
    await h.free((3, 2))
    assert await h.alloc(2, simultaneous=(EMPTY,)) == (0, 2)
    assert h.stats[:2] == (8, 4)
  simulate(4, scenario, grants=(False, True))


def test_statistics_and_reset_priority():
  async def scenario(h):
    await h.alloc(2, reset=True)
    assert h.stats == (0, 0, 0), "reset must beat bump updates"
    await h.idle()
    assert h.stats == (0, 0, 4)
    await h.idle(reset=True)
    assert h.stats == (0, 0, 0), "reset must beat the old bump watermark"
    await h.free((0,), reset=True)
    assert h.stats == (0, 0, 0)
    await h.free((1,), reset="link")
    assert h.stats[:2] == (0, 0), "link-edge reset must clear prior free accounting"
    assert await h.alloc(1, reset="read") == (0,)
    assert h.stats[:2] == (0, 0), "response-edge reset must beat pair-pop accounting"
    await h.free((0, 1), reset=True)
    await h.free((2, 3), reset=True)
    assert h.stats == (0, 0, 0)
    await h.alloc(2, reset=True)
    assert h.stats == (0, 0, 0)
    await h.idle()
    assert h.stats == (0, 0, 4), "reset clears stats, not allocation state"
    await h.free((0, 1))
    await h.alloc(1)
    assert h.stats == (2, 2, 4)
  simulate(4, scenario, grants=(False, True, False, True))


def test_capacity_boundaries():
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", UnusedElaboratable)
    try:
      for capacity in (-2, 0, 1, 3, ADDR_MAX, ADDR_MAX + 1):
        try:
          rejected = Alloc(capacity)
          del rejected
        except (AssertionError, ValueError):
          pass
        else:
          raise AssertionError(f"accepted invalid capacity {capacity}")
    finally:
      _ = gc.collect()

  async def tiny(h):
    await h.alloc(4)
    assert h.oom
    assert await h.alloc(1) == (0,)
    await h.free((1,))
    await h.alloc(1)
    assert h.oom
    await h.free((0,))
    await h.alloc(2)
    assert h.oom and h.oracle.free == [0]
    assert await h.alloc(1) == (0,)
    assert h.oracle.live == {0, 1}

  async def maximum(h):
    while h.oracle.bump < h.oracle.capacity:
      await h.alloc(min(ALLOC_LANES, (h.oracle.capacity - h.oracle.bump) // 2))
    assert h.oracle.bump == 1022
    await h.alloc(1)
    await h.free((1021,))
    await h.alloc(1)
    assert h.oom
    await h.free((1020,))
    assert await h.alloc(1) == (1020,)
    await h.free((1020, 1021))
    assert await h.alloc(1) == (1020,)
  assert ADDR_MAX == 1023
  run_cases((("tiny", lambda: simulate(2, tiny)),
             ("maximum", lambda: simulate(ADDR_MAX - 1, maximum, grants=(False, True)))))


def test_randomized_ownership_and_reuse():
  def case(seed):
    async def scenario(h):
      rng = random.Random(seed)
      for _ in range(160):
        if h.oracle.live and rng.randrange(5) < 3:
          count = rng.randint(1, min(FREE_LANES, len(h.oracle.live)))
          await h.free(rng.sample(sorted(h.oracle.live), count))
        else:
          await h.alloc(rng.randint(1, ALLOC_LANES))
      while h.oracle.live:
        await h.free(rng.sample(sorted(h.oracle.live), min(FREE_LANES, len(h.oracle.live))))
      assert 2 * len(h.oracle.free) == h.oracle.bump
      while h.oracle.free or h.oracle.bump < h.oracle.capacity:
        available = 2 * len(h.oracle.free) + h.oracle.capacity - h.oracle.bump
        await h.alloc(min(ALLOC_LANES, available // 2))
      assert h.oracle.live == set(range(h.oracle.capacity))
    simulate(30, scenario, grants=(False, True, False, False, True, True))
  run_cases((f"seed={seed}", lambda seed=seed: case(seed)) for seed in (0, 17, 2901))


def test_competing_alloc_clients():
  """The loser holds its request; only the accepted owner receives completion."""
  from amaranth.lib import wiring

  m = Module()
  dut = m.submodules.alloc = Alloc(16)
  arb = m.submodules.arb = AllocArbiter(2)
  wiring.connect(m, arb.real, dut.control)
  sim = Simulator(m)
  sim.add_clock(PERIOD)

  async def bench(ctx):
    h = Harness(ctx, dut, grants=(False, True, False, True), reset_port=arb.clients[0].stat_reset)

    async def round_of_requests(counts):
      waiting, active, received = {0, 1}, None, []
      for client, count in zip(arb.clients, counts):
        ctx.set(client.req_alloc, count)
      for _ in range(200):
        winners = [i for i in waiting if ctx.get(arb.clients[i].granted)]
        assert len(winners) <= 1
        if winners:
          assert active is None
          active = winners[0]
          h.begin("alloc", count=counts[active])
        completed = await h.tick()
        if winners:
          waiting.remove(winners[0])
          ctx.set(arb.clients[winners[0]].req_alloc, 0)
        if completed:
          assert active is not None
          for i, client in enumerate(arb.clients):
            assert bool(ctx.get(client.done)) == (i == active), "completion routed to wrong client"
          result = h.finish(counts[active])
          assert tuple(ctx.get(arb.clients[active].alloc_bases[i]) for i in range(counts[active])) == result
          assert bool(ctx.get(arb.clients[active].oom)) == h.oom
          received.extend(result)
          active = None
        if not waiting and active is None:
          break
      else:
        raise AssertionError("competing clients starved/timed out")
      assert len(set(words(received))) == 2 * sum(counts)
      return received

    first = await round_of_requests((4, 4))
    for base in first:
      _ = await h.free((base, base ^ 1), control=arb.clients[0])
      assert ctx.get(arb.clients[0].done) and not ctx.get(arb.clients[1].done)
    assert set(await round_of_requests((3, 2))) <= set(first)
    assert len(h.oracle.free) == 3
    assert not await h.tick()
    assert not ctx.get(dut.done) and not h.heap.pending and not h.heap.expected
    h.assert_minimum_timing()

  sim.add_testbench(bench)
  sim.run()


def verify_arbitrary_bump_requests():
  """Optional symbolic proof: single-edge bump/OOM for arbitrary valid pairs."""
  from amaranth import Assert, Assume, Cover, Mux, Signal
  from amaranth.asserts import AnySeq

  from ivga import Addr
  from ivga.verify import Verify

  dut = Alloc(capacity=6)
  verify = Verify(dut, cycles=9)
  m = verify.m
  request = AnySeq(len(dut.req_alloc))
  expected_empty = Signal(range(dut.capacity + 1))
  expected_done, expected_oom = Signal(), Signal()
  expected_base = Signal(Addr)
  expected_count = Signal(range(ALLOC_LANES + 1))
  expected_peak = Signal(Addr)
  oom = request * 2 > dut.capacity - expected_empty
  m.d.comb += [
    Assume(request <= ALLOC_LANES),
    dut.req_alloc.eq(request), dut.req_free.eq(0), dut.free_addrs.eq(0),
    dut.stat_reset.eq(0), dut.heap.granted.eq(0), dut.heap.read_data.eq(POISON),
  ]
  m.d.sync += [
    expected_done.eq(0), expected_oom.eq(0),
    expected_peak.eq(Mux(expected_empty > expected_peak, expected_empty, expected_peak)),
  ]
  with m.If(request != 0):
    with m.If(oom):
      m.d.sync += [expected_done.eq(1), expected_oom.eq(1)]
    with m.Else():
      m.d.sync += [
        expected_done.eq(1), expected_base.eq(expected_empty), expected_count.eq(request),
        expected_empty.eq(expected_empty + 2 * request),
      ]
  m.d.comb += [
    Assert(dut.granted), Assert(dut.empty == expected_empty),
    Assert(dut.empty <= dut.capacity), Assert(dut.empty[0] == 0),
    Assert(dut.done == expected_done), Assert(dut.oom == expected_oom),
    Assert(dut.free_head == ADDR_EMPTY), Assert(dut.free_count == 0),
    Assert(dut.stat_allocs == expected_empty), Assert(dut.stat_frees == 0),
    Assert(dut.stat_peak == expected_peak), Assert(~dut.heap.read), Assert(~dut.heap.write),
    Cover(dut.done & dut.oom), Cover(~dut.done),
  ]
  for lane in range(ALLOC_LANES):
    with m.If(expected_done & ~expected_oom & (lane < expected_count)):
      m.d.comb += Assert(dut.alloc_bases[lane] == expected_base + 2 * lane)
  for count in (1, 2, 3):
    m.d.comb += Cover(dut.done & ~dut.oom & (expected_count == count))
  verify.prove(depth=10)


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  _ = parser.add_argument("--formal", action="store_true", help="also prove the symbolic bump invariant (Yosys/SMTBMC/Z3)")
  args = parser.parse_args()
  tests = [
    test_minimum_schedule, test_minimum_cycle_timing, test_invalid_request_assertions,
    test_bump_and_atomic_oom, test_individual_word_reuse,
    test_mixed_reuse_and_atomic_oom, test_eight_word_batches_and_poisoned_inputs,
    test_free_acceptance_edges, test_allocation_priority, test_statistics_and_reset_priority,
    test_capacity_boundaries, test_randomized_ownership_and_reuse, test_competing_alloc_clients,
  ]
  if args.formal:
    tests.append(verify_arbitrary_bump_requests)
  timing, functional = [], []
  for test in tests:
    try:
      test()
    except TimingFailure as error:
      timing.append(test.__name__)
      print(f"FAIL {test.__name__} (timing): {error}", flush=True)
    except (AssertionError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
      functional.append(test.__name__)
      print(f"FAIL {test.__name__} (functional/tool): {error}", flush=True)
    else:
      print(f"PASS {test.__name__}", flush=True)
  print(f"Allocator test groups: {len(tests) - len(timing) - len(functional)} passed, "
        f"{len(timing)} timing failures, {len(functional)} functional/tool failures")
  return bool(timing or functional)


if __name__ == "__main__":
  raise SystemExit(main())
