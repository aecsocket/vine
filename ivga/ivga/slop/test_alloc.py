"""Paired-word allocator contract tests.

From ivga/: python -m ivga.slop.test_alloc
            python -m ivga.slop.test_alloc --formal

The default runs deterministic Amaranth simulations; only --formal needs the
existing Yosys/SMTBMC/Z3 toolchain. No VCDs or formal subprocesses are required
for normal use. The symbolic bump invariant is retained as an optional proof.

Unlike the old word-granular, held-until-done scripts, allocations count WORDS
but return aligned (base, base+1) pairs. Odd/oversized allocations fail atomically.
Frees still count individual words, processed in reverse input order. A stranded
half is not allocatable: only the second free pushes an even base onto the list.
Heap requests are accepted exactly once on req && granted, may change immediately
after acceptance, and receive ordered, possibly delayed responses. The model
starts with poisoned (not zeroed) heap contents and checks metadata only; freed
user data is unspecified. Invalid/double frees are deliberately not generated.
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

EMPTY = ADDR_EMPTY.value
POISON = 0xDEAD_BEEF_CAFE_F00D
PERIOD = 1e-6


@dataclass
class Oracle:
  """Word ownership and an independent stack of completely reclaimed pairs."""

  capacity: int
  bump: int = 0
  free: list[int] = field(default_factory=list)
  live: set[int] = field(default_factory=set)
  halves: set[int] = field(default_factory=set)

  @property
  def half_bits(self):
    return sum(1 << (addr // 2) for addr in self.halves)

  @property
  def head(self):
    return self.free[-1] if self.free else EMPTY

  def allocate(self, count):
    available = self.capacity - self.bump + 2 * len(self.free)
    oom = count % 2 != 0 or count > LANES or count > available
    if oom:
      return True, (), []
    assert 0 < count <= LANES
    result, transactions = [], []
    for _ in range(min(count // 2, len(self.free))):
      base = self.free.pop()
      transactions.append(("read", base, None))
      result.extend((base, base + 1))
    remaining = count - len(result)
    result.extend(range(self.bump, self.bump + remaining))
    self.bump += remaining
    assert not set(result) & self.live
    assert not set(result) & self.halves
    self.live.update(result)
    return False, tuple(result), transactions

  def release(self, addresses):
    assert 0 < len(addresses) <= LANES
    assert len(set(addresses)) == len(addresses)
    assert set(addresses) <= self.live, "test attempted an invalid/double free"
    transactions, half_states = [], []
    for addr in reversed(addresses):
      self.live.remove(addr)
      mate = addr ^ 1
      if mate in self.halves:
        self.halves.remove(mate)
        base = addr & ~1
        transactions.append(("write", base, self.head))
        self.free.append(base)
      else:
        self.halves.add(addr)
      half_states.append(self.half_bits)
    return transactions, half_states


@dataclass
class Response:
  kind: str
  addr: int
  value: int
  due: int


class HeapModel:
  """Ordered response queue, independent grant stalls, exact-once scoreboard.

  Writes are recorded at acceptance, not on response. In particular a producer
  keeping an already accepted request asserted is treated as issuing it AGAIN.
  Reads sample memory at acceptance; no allocator user data is initialized.
  """

  def __init__(self, capacity, grants=(True,), delays=(1,)):
    assert any(grants) and min(delays) >= 1
    self.grants, self.delays = grants, delays
    self.words = [POISON ^ (i * 0x10001) for i in range(capacity)]
    self.pending = deque()
    self.expected = deque()
    self.accepted = []
    self.responded = []
    self.stalled = None
    self.stall_cycles = 0
    self.max_pending = 0

  def drive(self, ctx, port, cycle):
    response = self.pending[0] if self.pending and self.pending[0].due <= cycle else None
    ctx.set(port.granted, self.grants[cycle % len(self.grants)])
    ctx.set(port.done, response is not None)
    ctx.set(port.read_data, response.value if response and response.kind == "read" else POISON)
    return response

  def accept(self, ctx, port, cycle, response):
    read, write = ctx.get(port.read), ctx.get(port.write)
    assert not (read and write), "allocator must not issue heap swaps"
    request = None
    if read or write:
      addr = ctx.get(port.addr)
      assert 0 <= addr < len(self.words) and addr % 2 == 0, ("unaligned heap request", addr)
      request = ("read" if read else "write", addr, ctx.get(port.write_data) if write else None)
    if self.stalled is not None:
      assert request == self.stalled, ("payload changed/withdrawn before grant", self.stalled, request)
    granted = bool(ctx.get(port.granted))
    self.stalled = request if request and not granted else None
    self.stall_cycles += int(self.stalled is not None)
    if response:
      assert self.pending.popleft() is response
      self.responded.append(response.kind)
    if not request or not granted:
      return None
    assert self.expected, ("unexpected/reissued heap transaction", request, cycle)
    expected = self.expected.popleft()
    assert request == expected, ("wrong heap transaction", expected, request, cycle)
    kind, addr, data = request
    if kind == "read":
      assert not any(item.kind == "read" for item in self.pending), "multiple outstanding reads"
    value = self.words[addr]
    if kind == "write":
      assert data is not None
      self.words[addr] = data
    delay = self.delays[len(self.accepted) % len(self.delays)]
    self.pending.append(Response(kind, addr, value, cycle + delay))
    self.accepted.append(request)
    self.max_pending = max(self.max_pending, len(self.pending))
    return request


class Harness:
  def __init__(self, ctx, dut, *, grants=(True,), delays=(1,), reset_port=None):
    assert LANES == 8
    assert hasattr(dut, "half_free"), "paired allocator core not available (missing half_free)"
    assert len(dut.half_free) == dut.capacity // 2
    self.ctx, self.dut = ctx, dut
    self.reset_port = dut.stat_reset if reset_port is None else reset_port
    self.oracle = Oracle(dut.capacity)
    self.heap = HeapModel(dut.capacity, grants, delays)
    self.cycle = 0
    self.stats = (0, 0, 0)
    self.half_states = []
    self.free_progress = 0
    self.kind = None
    self.busy_cycles = 0
    self.free_write_response_cycles = 0
    self.final_half_response_pending_counts = []
    self.final_half_pending_cycles = 0
    self.check_state()

  def state(self):
    ctx, dut = self.ctx, self.dut
    return (ctx.get(dut.empty), ctx.get(dut.free_count), ctx.get(dut.free_head), ctx.get(dut.half_free))

  def check_state(self):
    bump, count, head, halves = self.state()
    assert 0 <= bump <= self.dut.capacity and bump % 2 == 0, ("bump", bump)
    assert 0 <= count <= bump and count % 2 == 0, ("free_count", count)
    assert (head == EMPTY) == (count == 0), ("head/count mismatch", head, count)
    assert head == EMPTY or (head % 2 == 0 and head < bump), ("head", head)
    assert halves >> (bump // 2) == 0, "half_free above bump watermark"
    assert count + halves.bit_count() <= bump

  async def tick(self, reset: bool | Literal["read", "write"] = False):
    ctx, dut = self.ctx, self.dut
    response = self.heap.drive(ctx, dut.heap, self.cycle)
    # Event-targeted resets exercise priority over simultaneous operation updates.
    if reset == "read":
      reset = response is not None and response.kind == "read"
    elif reset == "write":
      reset = bool(ctx.get(dut.heap.write) and ctx.get(dut.heap.granted))
    ctx.set(self.reset_port, bool(reset))
    before = self.state()
    pending_before = len(self.heap.pending)
    accepted = self.heap.accept(ctx, dut.heap, self.cycle, response)
    write = int(accepted is not None and accepted[0] == "write")
    read = int(response is not None and response.kind == "read")
    await ctx.tick()
    self.cycle += 1
    after = self.state()
    self.check_state()
    assert after[1] == before[1] + 2 * write - 2 * read, "free_count must change at acceptance/read response"
    expected_head = before[2]
    if accepted is not None and accepted[0] == "write":
      expected_head = accepted[1]
    elif response is not None and response.kind == "read":
      expected_head = response.value
    assert after[2] == expected_head, "free_head must change at acceptance/read response"
    bump = after[0] - before[0]
    assert bump >= 0 and bump % 2 == 0
    freed = after[3].bit_count() - before[3].bit_count() + 2 * write
    assert freed >= 0, "half_free cleared without accepted metadata write"
    if self.kind == "free":
      self.free_progress += freed
      assert self.free_progress <= len(self.half_states)
      expected_half = self.half_states[self.free_progress - 1] if self.free_progress else before[3]
      assert after[3] == expected_half, "free inputs must be processed in captured reverse order"
      assert bump == 0 and read == 0
      responding_write = response is not None and response.kind == "write"
      self.free_write_response_cycles += int(bool(write) and responding_write)
      # A first-half free has no write acceptance. Record the final input word's
      # overlap explicitly so directed tests cannot silently miss this edge case.
      if freed == 1 and not write and self.free_progress == len(self.half_states):
        if responding_write:
          self.final_half_response_pending_counts.append(pending_before)
        elif pending_before:
          self.final_half_pending_cycles += 1
    else:
      assert freed == 0 and after[3] == before[3], "allocation/idle changed stranded halves"
    allocs, frees, peak = self.stats
    self.stats = (0, 0, 0) if reset else (allocs + bump + 2 * read, frees + freed, max(peak, before[0]))
    actual_stats = tuple(ctx.get(signal) for signal in (dut.stat_allocs, dut.stat_frees, dut.stat_peak))
    assert actual_stats == self.stats, ("statistics/reset/peak lag", self.cycle, actual_stats, self.stats)
    if ctx.get(dut.done):
      assert not self.heap.pending, "done before all accepted heap responses drained"
      assert not self.heap.expected, "done before all pair metadata transactions accepted"
    if not ctx.get(dut.granted):
      self.busy_cycles += 1
    return bool(ctx.get(dut.done))

  def begin(self, kind, count=0, addresses=()):
    assert not self.heap.pending and not self.heap.expected and self.heap.stalled is None
    self.kind = kind
    self.free_progress = 0
    if kind == "alloc":
      self.oom, self.result, transactions = self.oracle.allocate(count)
      self.half_states = []
    else:
      self.oom, self.result = False, ()
      transactions, self.half_states = self.oracle.release(addresses)
    self.heap.expected.extend(transactions)
    return len(transactions)

  def finish(self, count=0):
    ctx, dut, oracle = self.ctx, self.dut, self.oracle
    assert ctx.get(dut.done)
    assert bool(ctx.get(dut.oom)) == self.oom
    assert self.state() == (oracle.bump, 2 * len(oracle.free), oracle.head, oracle.half_bits), ("oracle", self.state(), oracle)
    if self.kind == "free":
      assert self.free_progress == len(self.half_states), "done before processing every free word"
    result = ()
    if self.kind == "alloc" and not self.oom:
      result = tuple(ctx.get(dut.alloc_addrs[i]) for i in range(count))
      assert len(set(result)) == count
      assert set(result) == set(self.result), ("ownership/free-first allocation", result, self.result)
      for i in range(0, count, 2):
        assert result[i] % 2 == 0 and result[i + 1] == result[i] + 1, ("unpaired result", result)
    self.kind = None
    return result

  async def operation(self, kind, count=0, addresses=(), *, poison=True, reset=False, simultaneous=()):
    ctx, dut = self.ctx, self.dut
    assert ctx.get(dut.granted), "control must be ready when idle"
    transactions = self.begin(kind, count, addresses)
    ctx.set(dut.req_alloc, count if kind == "alloc" else 0)
    frees = simultaneous if kind == "alloc" else addresses
    ctx.set(dut.req_free, len(frees))
    ctx.set(dut.free_addrs, list(frees) + [EMPTY] * (LANES - len(frees)))
    completed = await self.tick(reset=reset)
    # Pure bump and all rejected requests must complete on the request edge.
    if kind == "alloc" and not transactions:
      assert completed, "bump/atomic OOM must take one edge"
    for _ in range(2048):
      if completed:
        break
      assert not ctx.get(dut.granted), "busy allocator accepted a new control request"
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
      assert not self.ctx.get(self.dut.oom)
      assert self.ctx.get(self.dut.granted)
      assert not self.ctx.get(self.dut.heap.read) and not self.ctx.get(self.dut.heap.write)


def simulate(capacity, scenario, *, grants=(True,), delays=(1,)):
  dut = Alloc(capacity)
  sim = Simulator(dut)
  sim.add_clock(PERIOD)

  async def bench(ctx):
    h = Harness(ctx, dut, grants=grants, delays=delays)
    assert ctx.get(dut.granted) and not ctx.get(dut.done) and not ctx.get(dut.oom)
    await scenario(h)
    await h.idle(2)
    assert len(h.heap.accepted) == len(h.heap.responded)

  sim.add_testbench(bench)
  sim.run()


def test_bump_and_atomic_oom():
  async def scenario(h):
    await h.idle(3)
    # Spare encodings of the count signal (>8), not just odd legal-width counts.
    invalid = [n for n in range(1, 1 << len(h.dut.req_alloc)) if n % 2 or n > LANES]
    for n in invalid:
      await h.alloc(n)
      assert h.oracle.bump == 0
    assert await h.alloc(2) == (0, 1)
    assert h.stats == (2, 0, 0), "peak must lag the first bump by one edge"
    assert set(await h.alloc(4)) == set(range(2, 6))
    assert set(await h.alloc(6)) == set(range(6, 12))
    assert set(await h.alloc(8)) == set(range(12, 20))
    for _ in range(3):
      await h.alloc(2)
    assert h.stats[:2] == (20, 0)
    assert not h.heap.accepted
  simulate(20, scenario)


def test_partial_halves_and_coalescence():
  async def scenario(h):
    await h.alloc(4)
    await h.free((0,))
    await h.free((2,))
    assert h.oracle.half_bits == 0b11 and not h.oracle.free
    assert not h.heap.accepted, "first halves must not touch heap"
    await h.alloc(2)
    await h.alloc(4)
    assert h.stats[:2] == (4, 2), "stranded halves cannot satisfy allocation"
    # Even then odd, and odd then even; each is reclaimed only on the mate.
    await h.free((1,))
    assert await h.alloc(2) == (0, 1)
    await h.free((3,))
    assert await h.alloc(2) == (2, 3)
    await h.free((1,))
    await h.free((0,))
    assert await h.alloc(2) == (0, 1)
    await h.free((3,))
    await h.alloc(2)  # only half of this pair is free
    await h.free((2,))
    assert await h.alloc(2) == (2, 3)
    assert h.stats[:2] == (12, 8)
  simulate(4, scenario, grants=(False, False, True), delays=(5, 2))


def test_mixed_reuse_and_atomic_oom():
  async def scenario(h):
    await h.alloc(8)
    await h.free((5, 2, 4, 3))
    # Reverse order is 3, 4, 2, 5: bases 2 then 4 become complete.
    assert h.oracle.free == [2, 4], "reverse free order defines metadata stack"
    # Invalid requests must not pop available pairs or advance the bump.
    for n in (1, 3, 5, 7, 9, 14, 15):
      await h.alloc(n)
    before = len(h.heap.accepted)
    result = await h.alloc(8)
    assert set(result) == {2, 3, 4, 5, 8, 9, 10, 11}
    assert len(h.heap.accepted) - before == 2
    await h.free((0, 6))
    # No bump capacity and two stranded halves are not a reusable pair.
    await h.alloc(2)
    await h.free((1,))
    await h.alloc(4)  # one whole free pair is still insufficient; atomic OOM
    assert h.oracle.free == [0]
    assert await h.alloc(2) == (0, 1)
    await h.free((7,))
    assert await h.alloc(2) == (6, 7)
  simulate(12, scenario, grants=(True, False, False, True), delays=(7, 1, 4))


def test_eight_word_batches_and_poisoned_inputs():
  async def scenario(h):
    for _ in range(3):
      await h.alloc(8)
    for addresses in ((0, 9, 2, 11, 4, 13, 6, 15), (1, 8, 3, 10, 5, 12, 7, 14), tuple(range(16, 24))):
      await h.free(addresses)
    assert h.stats[:2] == (24, 24)
    assert h.oracle.half_bits == 0 and len(h.oracle.free) == 12
    for _ in range(3):
      await h.alloc(8)
    assert h.stats[:2] == (48, 24)
    assert sum(kind == "read" for kind, _, _ in h.heap.accepted) == 12
    assert sum(kind == "write" for kind, _, _ in h.heap.accepted) == 12
    assert h.heap.stall_cycles and h.busy_cycles
  simulate(24, scenario, grants=(False, False, True, True, False), delays=(11, 2, 5, 1))


def test_free_response_edges():
  async def overlapping_writes(h):
    await h.alloc(8)
    await h.free((0, 1, 2, 3))
    assert h.free_write_response_cycles == 1, "must accept a write while responding to its predecessor"
    assert h.heap.accepted == [("write", 2, EMPTY), ("write", 0, 2)]
    assert set(await h.alloc(4)) == {0, 1, 2, 3}

  simulate(8, overlapping_writes, delays=(2,))

  async def stalled_overlap(h):
    await overlapping_writes(h)
    assert h.heap.stall_cycles, "overlapping write must also exercise held-until-granted payload"

  simulate(8, stalled_overlap, grants=(True, False, True), delays=(3,))

  async def final_half_last_response(h):
    await h.alloc(8)
    # Reverse order 1, 0, 4: the write at 0 responds while the final free
    # strands word 4. No extra write/response may be required for completion.
    await h.free((4, 0, 1))
    assert h.final_half_response_pending_counts == [1], "must process final first-half free with last response"
    assert h.heap.accepted == [("write", 0, EMPTY)]
    assert h.stats[:2] == (8, 3)
    assert h.oracle.half_bits == 1 << 2
    assert await h.alloc(2) == (0, 1)
    await h.alloc(2)  # stranded word 4 is still unavailable

  simulate(8, final_half_last_response, delays=(1,))

  async def final_half_more_responses(h):
    await h.alloc(8)
    # Reverse order 3, 2, 1, 0, 4: final word 4 coincides with the first
    # response, but a second accepted write must still drain before done.
    await h.free((4, 0, 1, 2, 3))
    assert h.final_half_response_pending_counts == [2], "must leave another response outstanding"
    assert h.heap.max_pending == 2
    assert h.heap.accepted == [("write", 2, EMPTY), ("write", 0, 2)]
    assert h.stats[:2] == (8, 5)
    assert h.oracle.half_bits == 1 << 2
    assert set(await h.alloc(4)) == {0, 1, 2, 3}
    await h.alloc(2)

  simulate(8, final_half_more_responses, delays=(3,))

  async def final_half_waits(h):
    await h.alloc(8)
    await h.free((4, 0, 1))
    assert h.final_half_pending_cycles == 1, "final first-half free must wait for a delayed accepted write"
    assert not h.final_half_response_pending_counts
    assert h.heap.accepted == [("write", 0, EMPTY)]
    assert await h.alloc(2) == (0, 1)

  simulate(8, final_half_waits, delays=(5,))


def test_allocation_priority():
  async def scenario(h):
    await h.alloc(4)
    # A simultaneously valid free must be ignored, including on allocation OOM.
    await h.alloc(2, simultaneous=(0, 1))
    await h.alloc(3, simultaneous=(2, 3))
    assert h.oracle.live == {0, 1, 2, 3} and h.stats[:2] == (4, 0)
    await h.free((0, 1, 2, 3))
    assert set(await h.alloc(4, simultaneous=(EMPTY,))) == {0, 1, 2, 3}
    assert h.stats[:2] == (8, 4)
  simulate(4, scenario, grants=(False, True), delays=(3,))


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
    assert h.stats[:2] == (0, 0), "reset must beat accepted second-half free"
    assert await h.alloc(2, reset="read") == (0, 1)
    assert h.stats == (0, 0, 0), "reset must beat a reuse read response"
    await h.free((0, 1, 2, 3), reset=True)
    assert h.stats == (0, 0, 0)
    await h.alloc(4, reset=True)
    assert h.stats == (0, 0, 0)
    await h.idle()
    assert h.stats == (0, 0, 4), "reset clears stats, not allocation state"
    await h.free((0, 1))
    await h.alloc(2)
    assert h.stats == (2, 2, 4)
  simulate(4, scenario, grants=(False, True, False, True), delays=(6, 2))


def test_capacity_boundaries():
  # Rejected constructors still create Amaranth MustUse objects. Collect them
  # under a narrow warning filter rather than leaking warnings into later tests.
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
    assert await h.alloc(2) == (0, 1)
    await h.free((1,))
    await h.alloc(2)
    await h.free((0,))
    assert await h.alloc(2) == (0, 1)
  simulate(2, tiny)

  async def maximum(h):
    while h.oracle.bump < h.oracle.capacity:
      count = min(LANES, h.oracle.capacity - h.oracle.bump)
      await h.alloc(count)
    assert h.oracle.bump == 1022
    await h.alloc(2)
    await h.free((1021,))
    await h.alloc(2)
    await h.free((1020,))
    assert await h.alloc(2) == (1020, 1021)
  assert ADDR_MAX == 1023
  simulate(ADDR_MAX - 1, maximum, grants=(False, True), delays=(4,))


def test_randomized_ownership_and_reuse():
  # Fixed seeds, no external property-testing dependency. Arbitrary lane/free
  # orders exercise partial pairs, mixed reuse, exhaustion, and odd free batches.
  for seed in (0, 17, 2901):
    async def scenario(h, seed=seed):
      rng = random.Random(seed)
      for _ in range(160):
        if h.oracle.live and rng.randrange(5) < 3:
          count = rng.randint(1, min(LANES, len(h.oracle.live)))
          addresses = rng.sample(sorted(h.oracle.live), count)
          await h.free(addresses)
        else:
          await h.alloc(rng.choice((1, 2, 3, 4, 6, 8, 9, 15)))
      while h.oracle.live:
        addresses = rng.sample(sorted(h.oracle.live), min(LANES, len(h.oracle.live)))
        await h.free(addresses)
      assert not h.oracle.halves and len(h.oracle.free) * 2 == h.oracle.bump
      while h.oracle.free or h.oracle.bump < h.oracle.capacity:
        available = 2 * len(h.oracle.free) + h.oracle.capacity - h.oracle.bump
        await h.alloc(min(LANES, available))
      assert h.oracle.live == set(range(h.oracle.capacity))
    simulate(30, scenario, grants=(False, True, False, False, True, True), delays=(1, 9, 2, 5))


def test_competing_alloc_clients():
  """The losing client holds its request; only the owner receives completion."""
  from amaranth.lib import wiring

  m = Module()
  dut = m.submodules.alloc = Alloc(16)
  arb = m.submodules.arb = AllocArbiter(2)
  wiring.connect(m, arb.real, dut.control)
  sim = Simulator(m)
  sim.add_clock(PERIOD)

  async def bench(ctx):
    h = Harness(ctx, dut, grants=(False, True, False, True), delays=(5, 2), reset_port=arb.real.stat_reset)

    async def round_of_requests():
      waiting = {0, 1}
      active = None
      received = []
      for client in arb.clients:
        ctx.set(client.req_alloc, 4)
      for _ in range(200):
        # The real ready-like grant also identifies the winning idle request.
        winners = [i for i in waiting if ctx.get(arb.clients[i].granted)]
        assert len(winners) <= 1
        if winners:
          assert active is None
          active = winners[0]
          _ = h.begin("alloc", count=4)
        completed = await h.tick()
        if winners:
          assert active is not None
          waiting.remove(active)
          ctx.set(arb.clients[active].req_alloc, 0)
        if completed:
          assert active is not None
          for i, client in enumerate(arb.clients):
            assert bool(ctx.get(client.done)) == (i == active), "completion routed to wrong client"
          result = h.finish(4)
          assert tuple(ctx.get(arb.clients[active].alloc_addrs[i]) for i in range(4)) == result
          received.extend(result)
          active = None
        if not waiting and active is None:
          break
      else:
        raise AssertionError("competing alloc clients starved/timed out")
      assert len(set(received)) == 8
      return received

    first = await round_of_requests()  # one-edge bump completions
    # Route one eight-word free through a real client, then compete for reuse.
    client = arb.clients[0]
    _ = h.begin("free", addresses=tuple(first))
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
    _ = h.finish()
    assert set(await round_of_requests()) == set(first)
    assert not await h.tick()
    assert not ctx.get(dut.done) and not h.heap.pending and not h.heap.expected

  sim.add_testbench(bench)
  sim.run()


def verify_arbitrary_bump_requests():
  """Optional symbolic proof: arbitrary representable counts, no frees/heap."""
  from amaranth import Assert, Cover, Mux, Signal
  from amaranth.asserts import AnySeq

  from ivga import Addr
  from ivga.verify import Verify

  dut = Alloc(capacity=6)
  verify = Verify(dut, cycles=7)
  m = verify.m
  request = AnySeq(len(dut.req_alloc))
  expected_empty = Signal(range(dut.capacity + 1))
  expected_done, expected_oom = Signal(), Signal()
  expected_base = Signal(Addr)
  expected_count = Signal(range(LANES + 1))
  expected_peak = Signal(Addr)
  valid = (request <= LANES) & (request[0] == 0) & (request <= dut.capacity - expected_empty)
  m.d.comb += [
    dut.req_alloc.eq(request), dut.req_free.eq(0), dut.free_addrs.eq(0),
    dut.stat_reset.eq(0), dut.heap.granted.eq(0), dut.heap.done.eq(0), dut.heap.read_data.eq(POISON),
  ]
  m.d.sync += [
    expected_done.eq(request != 0), expected_oom.eq((request != 0) & ~valid),
    expected_peak.eq(Mux(expected_empty > expected_peak, expected_empty, expected_peak)),
  ]
  with m.If((request != 0) & valid):
    m.d.sync += [
      expected_base.eq(expected_empty), expected_count.eq(request),
      expected_empty.eq(expected_empty + request),
    ]
  m.d.comb += [
    Assert(dut.granted), Assert(dut.empty == expected_empty),
    Assert(dut.empty <= dut.capacity), Assert(dut.empty[0] == 0),
    Assert(dut.done == expected_done), Assert(dut.oom == expected_oom),
    Assert(dut.free_head == ADDR_EMPTY), Assert(dut.free_count == 0), Assert(dut.half_free == 0),
    Assert(dut.stat_allocs == expected_empty), Assert(dut.stat_frees == 0),
    Assert(dut.stat_peak == expected_peak), Assert(~dut.heap.read), Assert(~dut.heap.write),
    Cover(dut.done & dut.oom), Cover(~dut.done),
  ]
  for lane in range(LANES):
    with m.If(expected_done & ~expected_oom & (lane < expected_count)):
      addr = dut.alloc_addrs[lane]
      m.d.comb += [Assert(addr >= expected_base), Assert(addr < expected_base + expected_count)]
      if lane % 2 == 0:
        m.d.comb += [Assert(~addr[0]), Assert(dut.alloc_addrs[lane + 1] == addr + 1)]
      for previous in range(lane):
        m.d.comb += Assert(addr != dut.alloc_addrs[previous])
  for count in (2, 4, 6):
    m.d.comb += Cover(dut.done & ~dut.oom & (expected_count == count))
  verify.prove(depth=8)


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  _ = parser.add_argument("--formal", action="store_true", help="also prove the symbolic bump invariant (Yosys/SMTBMC/Z3)")
  args = parser.parse_args()
  tests = [
    test_bump_and_atomic_oom, test_partial_halves_and_coalescence,
    test_mixed_reuse_and_atomic_oom, test_eight_word_batches_and_poisoned_inputs,
    test_free_response_edges, test_allocation_priority, test_statistics_and_reset_priority,
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
