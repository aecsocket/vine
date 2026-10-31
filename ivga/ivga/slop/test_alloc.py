"""Single-word allocator contract tests.

From ivga/: python -m ivga.slop.test_alloc
            python -m ivga.slop.test_alloc --formal

The default runs deterministic Amaranth simulations; only --formal needs the
existing Yosys/SMTBMC/Z3 toolchain. Allocations and frees count individual words,
including odd counts. Oversized requests fail with bad_request; insufficient
capacity fails atomically with oom. Frees push each word in reverse input order.

Heap requests are accepted exactly once on request && granted. Writes commit at
that edge; read data is available in the following cycle, with no done signal.
Grant stalls are independent of this fixed response latency. Poisoned heap data
and changing busy-time inputs check that the allocator retains accepted inputs.
Invalid/double frees are deliberately not generated, except rejected oversized
batches whose payload must not be inspected.

Timing is checked independently of the implementation, counting the acceptance
edge as edge 1. With an always-granting heap: bump/rejection takes 1 edge, freeing
N words takes N edges, and R metadata reads take R + 1 edges (including mixed
reuse/bump allocation). Requests must use every available grant starting on the
control acceptance edge. Only denied grants may extend these budgets; the last
read's response-consumption edge does not require a grant. These are cycle-count
bounds for the existing one-metadata-transaction-per-word protocol, not an Fmax
claim or a comparison with other allocator architectures.
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

from ivga import ADDR_MAX
from ivga.mem.alloc import ADDR_EMPTY, LANES, Alloc, AllocArbiter
from ivga.mem.heap import HeapPort

EMPTY = ADDR_EMPTY.value
POISON = 0xDEAD_BEEF_CAFE_F00D
PERIOD = 1e-6


@dataclass
class Oracle:
  """Independent word ownership and LIFO free-list model."""

  capacity: int
  bump: int = 0
  free: list[int] = field(default_factory=list)
  live: set[int] = field(default_factory=set)

  @property
  def head(self):
    return self.free[-1] if self.free else EMPTY

  def allocate(self, count):
    assert count > 0
    bad_request = count > LANES
    oom = not bad_request and count > self.capacity - self.bump + len(self.free)
    if bad_request or oom:
      return oom, bad_request, (), []
    result, transactions = [], []
    for _ in range(min(count, len(self.free))):
      addr = self.free.pop()
      transactions.append(("read", addr, None))
      result.append(addr)
    remaining = count - len(result)
    result.extend(range(self.bump, self.bump + remaining))
    self.bump += remaining
    assert len(set(result)) == count and not set(result) & self.live
    self.live.update(result)
    return False, False, tuple(result), transactions

  def release(self, addresses):
    assert addresses
    if len(addresses) > LANES:
      return True, []
    assert len(set(addresses)) == len(addresses)
    assert set(addresses) <= self.live, "test attempted an invalid/double free"
    transactions = []
    for addr in reversed(addresses):
      self.live.remove(addr)
      transactions.append(("write", addr, self.head))
      self.free.append(addr)
    return False, transactions


@dataclass
class Response:
  value: int
  due: int


class HeapModel:
  """Fixed-next-cycle reads, grant stalls, and exact-once metadata scoreboard.

  A request held after acceptance is another transaction, not a continuation.
  Writes commit on acceptance; read data is poisoned outside its response cycle.
  """

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

  def drive_data(self, ctx, port, cycle):
    response = self.pending[0] if self.pending and self.pending[0].due == cycle else None
    ctx.set(port.read_data, response.value if response else POISON)
    return response

  def drive(self, ctx, port, cycle):
    request = ctx.get(port.read) or ctx.get(port.write)
    ctx.set(port.granted, bool(request and self.grants[cycle % len(self.grants)]))
    return self.drive_data(ctx, port, cycle)

  def accept(self, ctx, port, cycle, response):
    read, write = ctx.get(port.read), ctx.get(port.write)
    assert not (read and write), "allocator must not issue heap swaps"
    request = None
    if read or write:
      addr = ctx.get(port.addr)
      assert 0 <= addr < len(self.words), ("out-of-range heap request", addr)
      request = ("read" if read else "write", addr, ctx.get(port.write_data) if write else None)
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
    assert request == expected, ("wrong heap transaction", expected, request, cycle)
    kind, addr, data = request
    if kind == "read":
      assert not self.pending, "multiple outstanding allocator reads"
      self.pending.append(Response(self.words[addr], cycle + 1))
    else:
      assert data is not None
      self.words[addr] = data
    self.accepted.append(request)
    self.accepted_cycles.append(cycle)
    return request


def minimum_schedule(start, transactions, grants):
  """Absolute grant cycles and post-edge completion time, independent of DUT activity.

  `start` is the cycle before the control acceptance edge. Read responses can
  forward directly into the next request, so linked reads can accept every edge.
  Only the final read needs a subsequent edge to register its successor/head.
  """
  assert any(grants)
  cycle = start
  accepted = []
  for _ in transactions:
    while not grants[cycle % len(grants)]:
      cycle += 1
    accepted.append(cycle)
    cycle += 1
  done = cycle + int(transactions[-1][0] == "read") if transactions else start + 1
  return tuple(accepted), done


class Harness:
  def __init__(self, ctx, dut, *, grants=(True,), reset_port=None):
    assert LANES == 8
    self.ctx, self.dut = ctx, dut
    self.reset_port = dut.stat_reset if reset_port is None else reset_port
    self.oracle = Oracle(dut.capacity)
    self.heap = HeapModel(dut.capacity, grants)
    self.cycle = 0
    self.stats = (0, 0, 0)
    self.kind = None
    self.busy_cycles = 0
    self.timing_checks = 0
    self.timing_failures = []
    self.check_state()

  def state(self):
    ctx, dut = self.ctx, self.dut
    return (ctx.get(dut.empty), ctx.get(dut.free_count), ctx.get(dut.free_head))

  def check_state(self):
    bump, count, head = self.state()
    assert 0 <= bump <= self.dut.capacity, ("bump", bump)
    assert 0 <= count <= bump, ("free_count", count)
    assert (head == EMPTY) == (count == 0), ("head/count mismatch", head, count)
    assert head == EMPTY or 0 <= head < bump, ("head", head)

  async def tick(self, reset: bool | Literal["read", "write"] = False):
    ctx, dut = self.ctx, self.dut
    response = self.heap.drive(ctx, dut.heap, self.cycle)
    # A read event is the response-consumption edge, not the request edge.
    if reset == "read":
      reset = response is not None
    elif reset == "write":
      reset = bool(ctx.get(dut.heap.write) and ctx.get(dut.heap.granted))
    ctx.set(self.reset_port, bool(reset))
    before = self.state()
    accepted = self.heap.accept(ctx, dut.heap, self.cycle, response)
    write = int(accepted is not None and accepted[0] == "write")
    read = int(response is not None)
    await ctx.tick()
    self.cycle += 1
    # Publish the synchronous memory response immediately after its grant edge.
    _ = self.heap.drive_data(ctx, dut.heap, self.cycle)
    after = self.state()
    self.check_state()
    assert after[1] == before[1] + write - read, ("free_count acceptance/response edge", before, after)
    expected_head = before[2]
    if accepted is not None and accepted[0] == "write":
      expected_head = accepted[1]
    elif response is not None:
      expected_head = response.value
    assert after[2] == expected_head, ("free_head acceptance/response edge", after[2], expected_head)
    bump = after[0] - before[0]
    assert bump >= 0
    if self.kind == "free":
      assert bump == 0 and read == 0
    else:
      assert not write, "allocation/idle wrote free-list metadata"
    allocs, frees, peak = self.stats
    self.stats = (0, 0, 0) if reset else (allocs + bump + read, frees + write, max(peak, before[0]))
    actual_stats = tuple(ctx.get(signal) for signal in (dut.stat_allocs, dut.stat_frees, dut.stat_peak))
    assert actual_stats == self.stats, ("statistics/reset/peak lag", self.cycle, actual_stats, self.stats)
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
      self.oom, self.bad_request, self.result, transactions = self.oracle.allocate(count)
    else:
      self.oom, self.result = False, ()
      self.bad_request, transactions = self.oracle.release(addresses)
    self.heap.expected.extend(transactions)
    self.timing_start = self.cycle
    self.transaction_start = len(self.heap.accepted_cycles)
    self.minimum_grants, self.minimum_done = minimum_schedule(self.cycle, transactions, self.heap.grants)
    if self.oom or self.bad_request:
      self.timing_path = f"rejected {kind}"
    elif kind == "free":
      self.timing_path = "free"
    elif not transactions:
      self.timing_path = "bump"
    else:
      self.timing_path = "reuse" if len(transactions) == count else "mixed reuse/bump"
    self.timing_count = count if kind == "alloc" else len(addresses)

  def finish(self, count=0):
    ctx, dut, oracle = self.ctx, self.dut, self.oracle
    assert ctx.get(dut.done)
    assert bool(ctx.get(dut.oom)) == self.oom, ("oom", self.oom)
    assert bool(ctx.get(dut.bad_request)) == self.bad_request, ("bad_request", self.bad_request)
    assert self.state() == (oracle.bump, len(oracle.free), oracle.head), ("oracle", self.state(), oracle)
    result = ()
    if self.kind == "alloc" and not (self.oom or self.bad_request):
      result = tuple(ctx.get(dut.alloc_addrs[i]) for i in range(count))
      assert len(set(result)) == count, ("duplicate allocation", result)
      assert set(result) == set(self.result), ("ownership/free-first allocation", result, self.result)
    actual_grants = tuple(self.heap.accepted_cycles[self.transaction_start:])
    self.timing_checks += 1
    if actual_grants != self.minimum_grants or self.cycle != self.minimum_done or not ctx.get(dut.granted):
      # Keep running functional checks so one slow bump does not hide slow frees
      # and reads. Report the performance failures together at scenario end.
      actual_edges = tuple(cycle - self.timing_start + 1 for cycle in actual_grants)
      minimum_edges = tuple(cycle - self.timing_start + 1 for cycle in self.minimum_grants)
      self.timing_failures.append((self.timing_path, (
        f"{self.timing_path}({self.timing_count}): done after {self.cycle - self.timing_start} edges "
        f"(minimum {self.minimum_done - self.timing_start}); "
        f"heap grant edges {actual_edges} (expected {minimum_edges}); "
        f"ready at done={bool(ctx.get(dut.granted))}"
      )))
    self.kind = None
    return result

  def assert_minimum_timing(self):
    examples = {}
    for path, message in self.timing_failures:
      examples.setdefault(path, message)
    assert not self.timing_failures, (
      f"nonminimal cycle timing in {len(self.timing_failures)}/{self.timing_checks} operations:\n  "
      + "\n  ".join(examples.values())
    )

  async def operation(self, kind, count=0, addresses=(), *, poison=True, reset=False, simultaneous=()):
    ctx, dut = self.ctx, self.dut
    assert ctx.get(dut.granted), "control must be ready when idle"
    self.begin(kind, count, addresses)
    ctx.set(dut.req_alloc, count if kind == "alloc" else 0)
    frees = simultaneous if kind == "alloc" else addresses
    ctx.set(dut.req_free, len(frees))
    ctx.set(dut.free_addrs, (list(frees) + [EMPTY] * LANES)[:LANES])
    completed = await self.tick(reset=reset)
    if self.oom or self.bad_request:
      assert completed, "rejected request must complete atomically on acceptance"
    for _ in range(2048):
      if completed:
        break
      assert not ctx.get(dut.granted), "busy allocator accepted a new control request"
      # Poison only while busy; clear before another ready edge can accept it.
      ctx.set(dut.req_alloc, LANES if poison else 0)
      ctx.set(dut.req_free, LANES if poison else 0)
      ctx.set(dut.free_addrs, [EMPTY] * LANES)
      completed = await self.tick(reset=reset)
    else:
      raise AssertionError("allocator timed out (2048 edges)")
    ctx.set(dut.req_alloc, 0)
    ctx.set(dut.req_free, 0)
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
      assert not self.ctx.get(self.dut.oom) and not self.ctx.get(self.dut.bad_request)
      assert self.ctx.get(self.dut.granted)
      assert not self.ctx.get(self.dut.heap.read) and not self.ctx.get(self.dut.heap.write)


def simulate(capacity, scenario, *, grants=(True,)):
  dut = Alloc(capacity, HeapPort().create())
  sim = Simulator(dut)
  sim.add_clock(PERIOD)

  async def bench(ctx):
    h = Harness(ctx, dut, grants=grants)
    assert ctx.get(dut.granted) and not ctx.get(dut.done) and not ctx.get(dut.oom)
    await scenario(h)
    await h.idle(2)
    assert not h.heap.pending and not h.heap.expected and h.heap.stalled is None
    assert sum(kind == "read" for kind, _, _ in h.heap.accepted) == h.heap.responded
    h.assert_minimum_timing()

  sim.add_testbench(bench)
  sim.run()


def test_minimum_schedule():
  writes = [("write", 0, EMPTY)] * 3
  reads = [("read", 0, None)] * 3
  assert minimum_schedule(4, [], (False, True)) == ((), 5)
  assert minimum_schedule(4, writes, (True,)) == ((4, 5, 6), 7)
  assert minimum_schedule(4, reads, (True,)) == ((4, 5, 6), 8)
  grants = (False, True, False, False, True)
  assert minimum_schedule(0, writes, grants) == ((1, 4, 6), 7)
  assert minimum_schedule(0, reads, grants) == ((1, 4, 6), 8)
  assert minimum_schedule(2, reads[:2], grants) == ((4, 6), 8)
  # Completion after the final read cannot depend on another available grant.
  assert minimum_schedule(1, reads[:1], (False, True, False)) == ((1,), 3)


def test_minimum_cycle_timing():
  """Every batch size, with and without stalls, including mixed reuse/bump."""
  async def scenario(h):
    await h.alloc(LANES + 1)  # Rejection must ignore heap backpressure.
    for count in range(1, LANES + 1):
      addresses = await h.alloc(count)
      await h.free(addresses)
      reused = await h.alloc(count)
      if count > 1:
        await h.free(reused[:-1])
        await h.alloc(count)  # Consume count-1 free words plus one bump word.

  failures = []
  for grants in ((True,), (False, True), (True, False), (False, False, True, True, False)):
    try:
      simulate(64, scenario, grants=grants)
    except AssertionError as error:
      failures.append(f"grants={grants}: {error}")
  assert not failures, "\n".join(failures)


def test_bump_and_atomic_oom():
  async def scenario(h):
    await h.idle(3)
    for n in range(LANES + 1, 1 << len(h.dut.req_alloc)):
      await h.alloc(n)
      assert h.oracle.bump == 0
    assert await h.alloc(1) == (0,)
    assert h.stats == (1, 0, 0), "peak must lag the first bump by one edge"
    assert set(await h.alloc(3)) == set(range(1, 4))
    assert set(await h.alloc(7)) == set(range(4, 11))
    assert set(await h.alloc(8)) == set(range(11, 19))
    await h.alloc(2)  # Insufficient capacity must not partially allocate word 19.
    assert h.oracle.bump == 19
    assert await h.alloc(1) == (19,)
    for _ in range(3):
      await h.alloc(2)
    assert h.stats[:2] == (20, 0)
    assert not h.heap.accepted
  simulate(20, scenario)


def test_individual_word_reuse():
  async def scenario(h):
    await h.alloc(4)
    await h.free((1,))
    assert h.oracle.free == [1]
    assert h.heap.accepted == [("write", 1, EMPTY)]
    assert await h.alloc(1) == (1,), "odd addresses are immediately reusable"
    await h.free((0,))
    await h.free((2,))
    assert set(await h.alloc(2)) == {0, 2}, "unrelated words need not coalesce"
    await h.free((3, 1, 0))
    assert set(await h.alloc(3)) == {0, 1, 3}
    await h.alloc(1)  # All four words are owned again.
    assert h.stats[:2] == (10, 6)
  simulate(4, scenario, grants=(False, False, True))


def test_mixed_reuse_and_atomic_oom():
  async def scenario(h):
    await h.alloc(8)
    await h.free((5, 2, 4, 3))
    assert h.oracle.free == [3, 4, 2, 5]
    for n in (9, 14, 15):
      await h.alloc(n)
    before = len(h.heap.accepted)
    assert set(await h.alloc(8)) == {2, 3, 4, 5, 8, 9, 10, 11}
    assert len(h.heap.accepted) - before == 4
    await h.free((0, 6))
    await h.alloc(3)  # Two reusable words are insufficient; do not pop either.
    assert h.oracle.free == [6, 0]
    assert set(await h.alloc(2)) == {0, 6}
    await h.free((1, 7, 3))
    assert set(await h.alloc(3)) == {1, 3, 7}
  simulate(12, scenario, grants=(True, False, False, True))


def test_eight_word_batches_and_poisoned_inputs():
  async def scenario(h):
    for _ in range(3):
      await h.alloc(8)
    for addresses in ((0, 9, 2, 11, 4, 13, 6, 15), (1, 8, 3, 10, 5, 12, 7, 14), tuple(range(16, 24))):
      await h.free(addresses)
    assert h.stats[:2] == (24, 24) and len(h.oracle.free) == 24
    for _ in range(3):
      await h.alloc(8)
    assert h.stats[:2] == (48, 24)
    assert sum(kind == "read" for kind, _, _ in h.heap.accepted) == 24
    assert sum(kind == "write" for kind, _, _ in h.heap.accepted) == 24
    assert h.heap.stall_cycles and h.busy_cycles
  simulate(24, scenario, grants=(False, False, True, True, False))


def test_free_acceptance_edges():
  async def consecutive(h):
    await h.alloc(8)
    await h.free((0, 1, 2, 3))
    assert h.heap.accepted == [
      ("write", 3, EMPTY), ("write", 2, 3), ("write", 1, 2), ("write", 0, 1),
    ]
    cycles = h.heap.accepted_cycles
    assert cycles == list(range(cycles[0], cycles[0] + 4)), "free writes must not insert bubbles"

    assert set(await h.alloc(4)) == {0, 1, 2, 3}
  simulate(8, consecutive)

  async def stalled(h):
    await h.alloc(8)
    await h.free((4, 0, 1))
    assert h.heap.accepted == [("write", 1, EMPTY), ("write", 0, 1), ("write", 4, 0)]
    assert h.heap.stall_cycles, "must hold metadata payload stable until granted"
    assert set(await h.alloc(3)) == {0, 1, 4}
    assert h.stats[:2] == (11, 3)
  simulate(8, stalled, grants=(True, False, False, True))


def test_allocation_priority():
  async def scenario(h):
    await h.alloc(4)
    # Even rejected allocations take priority over a simultaneously valid free.
    await h.alloc(2, simultaneous=(0, 1))
    await h.alloc(3, simultaneous=(2, 3))
    await h.alloc(9, simultaneous=(0,))
    assert h.oracle.live == {0, 1, 2, 3} and h.stats[:2] == (4, 0)
    await h.free((0, 1, 2, 3))
    assert set(await h.alloc(4, simultaneous=(EMPTY,))) == {0, 1, 2, 3}
    assert h.stats[:2] == (8, 4)
  simulate(4, scenario, grants=(False, True))


def test_statistics_and_reset_priority():
  async def scenario(h):
    await h.alloc(4, reset=True)
    assert h.stats == (0, 0, 0), "reset must beat the bump's stat updates"
    await h.idle()
    assert h.stats == (0, 0, 4)
    await h.idle(reset=True)
    assert h.stats == (0, 0, 0), "reset must beat the old bump watermark"
    await h.free((0,), reset=True)
    assert h.stats == (0, 0, 0)
    await h.free((1,), reset="write")
    assert h.stats[:2] == (0, 0), "write-edge reset must beat free accounting"
    assert set(await h.alloc(2, reset="read")) == {0, 1}
    assert h.stats[:2] == (0, 0), "read-consumption reset must beat reuse accounting"
    await h.free((0, 1, 2, 3), reset=True)
    assert h.stats == (0, 0, 0)
    await h.alloc(4, reset=True)
    assert h.stats == (0, 0, 0)
    await h.idle()
    assert h.stats == (0, 0, 4), "reset clears stats, not allocation state"
    await h.free((0, 1))
    await h.alloc(2)
    assert h.stats == (2, 2, 4)
  simulate(4, scenario, grants=(False, True, False, True))


def test_capacity_boundaries():
  # Rejected constructors still create Amaranth MustUse objects.
  with warnings.catch_warnings():
    warnings.simplefilter("ignore", UnusedElaboratable)
    try:
      for capacity in (-2, 0, 1, 3, ADDR_MAX, ADDR_MAX + 1):
        try:
          rejected = Alloc(capacity, HeapPort().create())
          del rejected
        except (AssertionError, ValueError):
          pass
        else:
          raise AssertionError(f"accepted invalid capacity {capacity}")
    finally:
      _ = gc.collect()

  async def tiny(h):
    await h.alloc(4)
    assert await h.alloc(1) == (0,)
    assert await h.alloc(1) == (1,)
    await h.free((1,))
    await h.alloc(2)  # Atomic OOM, even with one reusable word.
    assert await h.alloc(1) == (1,)
    # Spare count encodings must be rejected for frees as well as allocations.
    for count in range(LANES + 1, 1 << len(h.dut.req_free)):
      await h.free((EMPTY,) * count)
    assert h.oracle.live == {0, 1}
  simulate(2, tiny)

  async def maximum(h):
    while h.oracle.bump < h.oracle.capacity:
      await h.alloc(min(LANES, h.oracle.capacity - h.oracle.bump))
    assert h.oracle.bump == 1022
    await h.alloc(1)
    await h.free((1021,))
    assert await h.alloc(1) == (1021,)
    await h.free((1020, 1021))
    assert set(await h.alloc(2)) == {1020, 1021}
  assert ADDR_MAX == 1023
  simulate(ADDR_MAX - 1, maximum, grants=(False, True))


def test_randomized_ownership_and_reuse():
  for seed in (0, 17, 2901):
    async def scenario(h, seed=seed):
      rng = random.Random(seed)
      for _ in range(160):
        if h.oracle.live and rng.randrange(5) < 3:
          count = rng.randint(1, min(LANES, len(h.oracle.live)))
          await h.free(rng.sample(sorted(h.oracle.live), count))
        else:
          await h.alloc(rng.choice((1, 2, 3, 4, 6, 8, 9, 15)))
      while h.oracle.live:
        await h.free(rng.sample(sorted(h.oracle.live), min(LANES, len(h.oracle.live))))
      assert len(h.oracle.free) == h.oracle.bump
      while h.oracle.free or h.oracle.bump < h.oracle.capacity:
        available = len(h.oracle.free) + h.oracle.capacity - h.oracle.bump
        await h.alloc(min(LANES, available))
      assert h.oracle.live == set(range(h.oracle.capacity))
    simulate(30, scenario, grants=(False, True, False, False, True, True))


def test_competing_alloc_clients():
  """The losing client holds its request; only the owner receives completion."""
  from amaranth.lib import wiring

  m = Module()
  dut = m.submodules.alloc = Alloc(16, HeapPort().create())
  arb = m.submodules.arb = AllocArbiter(2)
  wiring.connect(m, arb.real, dut.control)
  sim = Simulator(m)
  sim.add_clock(PERIOD)

  async def bench(ctx):
    h = Harness(ctx, dut, grants=(False, True, False, True), reset_port=arb.clients[0].stat_reset)

    async def round_of_requests(counts=(4, 4)):
      waiting = {0, 1}
      active = None
      received = []
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
          assert active is not None
          waiting.remove(active)
          ctx.set(arb.clients[active].req_alloc, 0)
        if completed:
          assert active is not None
          for i, client in enumerate(arb.clients):
            assert bool(ctx.get(client.done)) == (i == active), "completion routed to wrong client"
          count = counts[active]
          result = h.finish(count)
          assert tuple(ctx.get(arb.clients[active].alloc_addrs[i]) for i in range(count)) == result
          received.extend(result)
          active = None
        if not waiting and active is None:
          break
      else:
        raise AssertionError("competing alloc clients starved/timed out")
      assert len(set(received)) == 8
      return received

    first = await round_of_requests()
    client = arb.clients[0]
    h.begin("free", addresses=tuple(first))
    ctx.set(client.req_free, 8)
    ctx.set(client.free_addrs, first)
    assert ctx.get(client.granted)
    completed = await h.tick()
    ctx.set(client.req_free, 0)
    for _ in range(200):
      if completed:
        break
      completed = await h.tick()
    else:
      raise AssertionError("arbiter free timed out")
    assert ctx.get(client.done) and not ctx.get(arb.clients[1].done)
    _ = h.finish()
    assert set(await round_of_requests((3, 5))) == set(first)
    assert not await h.tick()
    assert not ctx.get(dut.done) and not h.heap.pending and not h.heap.expected
    h.assert_minimum_timing()

  sim.add_testbench(bench)
  sim.run()


def verify_arbitrary_bump_requests():
  """Optional symbolic proof: single-edge bump/rejection for arbitrary counts."""
  from amaranth import Assert, Cover, Mux, Signal
  from amaranth.asserts import AnySeq

  from ivga import Addr
  from ivga.verify import Verify

  dut = Alloc(capacity=6, heap=HeapPort().create())
  verify = Verify(dut, cycles=9)
  m = verify.m
  request = AnySeq(len(dut.req_alloc))
  expected_empty = Signal(range(dut.capacity + 1))

  expected_done, expected_oom, expected_bad = Signal(), Signal(), Signal()
  expected_base = Signal(Addr)
  expected_count = Signal(range(LANES + 1))
  expected_peak = Signal(Addr)
  bad = request > LANES
  oom = ~bad & (request > dut.capacity - expected_empty)
  m.d.comb += [
    dut.req_alloc.eq(request), dut.req_free.eq(0), dut.free_addrs.eq(0),
    dut.stat_reset.eq(0), dut.heap.granted.eq(0), dut.heap.read_data.eq(POISON),
  ]
  m.d.sync += [
    expected_done.eq(0), expected_oom.eq(0), expected_bad.eq(0),
    expected_peak.eq(Mux(expected_empty > expected_peak, expected_empty, expected_peak)),
  ]
  with m.If(request != 0):
    with m.If(bad | oom):
      m.d.sync += [expected_done.eq(1), expected_oom.eq(oom), expected_bad.eq(bad)]
    with m.Else():
      m.d.sync += [
        expected_done.eq(1), expected_base.eq(expected_empty), expected_count.eq(request),
        expected_empty.eq(expected_empty + request),
      ]
  m.d.comb += [
    Assert(dut.granted), Assert(dut.empty == expected_empty),
    Assert(dut.empty <= dut.capacity),
    Assert(dut.done == expected_done), Assert(dut.oom == expected_oom),
    Assert(dut.bad_request == expected_bad),
    Assert(dut.free_head == ADDR_EMPTY), Assert(dut.free_count == 0),
    Assert(dut.stat_allocs == expected_empty), Assert(dut.stat_frees == 0),
    Assert(dut.stat_peak == expected_peak), Assert(~dut.heap.read), Assert(~dut.heap.write),
    Cover(dut.done & dut.oom), Cover(dut.done & dut.bad_request), Cover(~dut.done),
  ]
  for lane in range(LANES):
    with m.If(expected_done & ~expected_oom & ~expected_bad & (lane < expected_count)):
      m.d.comb += Assert(dut.alloc_addrs[lane] == expected_base + lane)
  for count in (1, 2, 3, 4, 5, 6):
    m.d.comb += Cover(dut.done & ~dut.oom & ~dut.bad_request & (expected_count == count))
  verify.prove(depth=10)


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  _ = parser.add_argument("--formal", action="store_true", help="also prove the symbolic bump invariant (Yosys/SMTBMC/Z3)")
  args = parser.parse_args()
  tests = [
    test_minimum_schedule, test_minimum_cycle_timing,
    test_bump_and_atomic_oom, test_individual_word_reuse,
    test_mixed_reuse_and_atomic_oom, test_eight_word_batches_and_poisoned_inputs,
    test_free_acceptance_edges, test_allocation_priority, test_statistics_and_reset_priority,
    test_capacity_boundaries, test_randomized_ownership_and_reuse, test_competing_alloc_clients,
  ]
  if args.formal:
    tests.append(verify_arbitrary_bump_requests)
  failures = []
  for test in tests:
    try:
      test()
    except (AssertionError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as error:
      failures.append(test.__name__)
      print(f"FAIL {test.__name__}: {error}", flush=True)
    else:
      print(f"PASS {test.__name__}", flush=True)
  print(f"Allocator test groups: {len(tests) - len(failures)} passed, {len(failures)} failed")
  return bool(failures)


if __name__ == "__main__":
  raise SystemExit(main())
