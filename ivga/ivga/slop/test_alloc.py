"""Batched allocator proofs: run `python -m ivga.slop.test_alloc`.

Requests are counts in 0..LANES; zero means idle. Only alloc_addrs[:n] is
valid, and its ordering is unspecified. Scripts use a Python oracle for the
current free-first policy, reverse-order batch frees, and delayed heap service.
OOM must be atomic. Busy-cycle input changes must not change captured requests.
Invalid counts, invalid frees, and duplicate frees are caller errors.
"""

import subprocess
from dataclasses import dataclass, field
from typing import TypedDict

from amaranth import Array, Assert, Assume, Cat, Const, Cover, Mux, Signal
from amaranth.asserts import AnySeq
from amaranth.hdl import Fragment

from ivga import Addr, Word
from ivga.mem.alloc import ADDR_EMPTY, IDLE, LANES, Alloc
from ivga.verify import Verify

EMPTY = ADDR_EMPTY.value
POISON = 0xDEAD_BEEF


def setup(capacity=4, cycles=5):
  dut = Alloc(capacity=capacity)
  for signal in (dut.stat_allocs, dut.stat_frees, dut.stat_peak):
    signal.attrs["keep"] = 1
  verify = Verify(dut, cycles=cycles)
  verify.m.d.comb += [
    dut.req_alloc.eq(0), dut.req_free.eq(0), dut.free_addrs.eq(0),
    dut.stat_reset.eq(False), dut.heap.done.eq(False), dut.heap.read_data.eq(0),
    Assert(dut.empty <= capacity),
    Assert(dut.free_count <= dut.empty),
    Assert((dut.free_head == ADDR_EMPTY) == (dut.free_count == 0)),
  ]
  return dut, verify


def add_heap_model(m, dut: Alloc, delay: int):
  """Capture once, acknowledge after delay edges, and assert request stability.

  Each transaction takes delay + 1 edges from its initial request through the
  allocator consuming its acknowledgment. Consecutive batch entries may change
  address/data after that consumption edge without a request-low bubble.
  """
  assert delay >= 1
  words = Array(Signal(Word, name=f"heap_{i}") for i in range(dut.capacity))
  pending = Signal()
  remaining = Signal(range(delay))
  read, write = Signal(), Signal()
  addr = Signal.like(dut.heap.addr)
  data = Signal(Word)
  reads, writes = Signal(16), Signal(16)
  request = dut.heap.read | dut.heap.write
  complete = pending & (remaining == 0)
  m.d.comb += [
    dut.heap.done.eq(complete),
    dut.heap.read_data.eq(Mux(complete & read, words[addr], POISON)),
    Assert(~(dut.heap.read & dut.heap.write)),
  ]
  with m.If(request):
    m.d.comb += Assert(dut.heap.addr < dut.capacity)
  with m.If(~pending & request):
    m.d.sync += [
      pending.eq(True), remaining.eq(delay - 1),
      read.eq(dut.heap.read), write.eq(dut.heap.write),
      addr.eq(dut.heap.addr), data.eq(dut.heap.write_data),
    ]
  with m.Elif(pending):
    m.d.comb += [
      Assert(dut.heap.read == read), Assert(dut.heap.write == write),
      Assert(dut.heap.addr == addr),
    ]
    with m.If(write):
      m.d.comb += Assert(dut.heap.write_data == data)
    with m.If(complete):
      m.d.sync += pending.eq(False)
      with m.If(write):
        m.d.sync += [words[addr].eq(data), writes.eq(writes + 1)]
      with m.If(read):
        m.d.sync += reads.eq(reads + 1)
    with m.Else():
      m.d.sync += remaining.eq(remaining - 1)
  return words, reads, writes


class ExpectedState(TypedDict):
  bump: int
  head: int
  free_count: int
  words: tuple[int, ...]
  allocs: int
  frees: int
  peak: int
  reads: int
  writes: int
  done: bool
  oom: bool


@dataclass
class Oracle:
  capacity: int
  bump: int = 0
  free: list[int] = field(default_factory=list)
  live: set[int] = field(default_factory=set)
  words: list[int] = field(init=False)
  allocs: int = 0
  frees: int = 0
  peak: int = 0
  reads: int = 0
  writes: int = 0

  def __post_init__(self):
    self.words = [0] * self.capacity

  def snapshot(self, *, done=False, oom=False) -> ExpectedState:
    return {
      "bump": self.bump, "head": self.free[-1] if self.free else EMPTY,
      "free_count": len(self.free), "words": tuple(self.words),
      "allocs": self.allocs, "frees": self.frees, "peak": self.peak,
      "reads": self.reads, "writes": self.writes, "done": done, "oom": oom,
    }

  def advance(self, *, bump=0, recycle=False, free_addr=None, done=False, oom=False) -> ExpectedState:
    # stat_peak observes the old bump pointer at each edge.
    self.peak = max(self.peak, self.bump)
    if recycle:
      addr = self.free.pop()
      assert addr not in self.live
      self.live.add(addr)
      self.allocs += 1
      self.reads += 1
    if bump:
      addresses = set(range(self.bump, self.bump + bump))
      assert not addresses & self.live
      assert self.bump + bump <= self.capacity
      self.live.update(addresses)
      self.bump += bump
      self.allocs += bump
    if free_addr is not None:
      assert free_addr in self.live, "scripts must only free live addresses"
      self.words[free_addr] = self.free[-1] if self.free else EMPTY
      self.live.remove(free_addr)
      self.free.append(free_addr)
      self.frees += 1
      self.writes += 1
    return self.snapshot(done=done, oom=oom)


def schedule_sequence(operations, capacity, heap_delay):
  """Return per-cycle expected state and the independently predicted results.

  Operations: ('alloc', n), ('free', addresses), ('idle', cycles), or
  ('both', n, free_addresses). Legacy single-word tuples are also accepted.
  """
  assert heap_delay >= 1
  oracle = Oracle(capacity)
  states = [oracle.snapshot()]
  schedule = []
  for operation in operations:
    kind = operation[0]
    if kind == "idle":
      for _ in range(operation[1]):
        states.append(oracle.advance())
      continue
    assert kind in ("alloc", "free", "both")
    start = len(states) - 1
    addresses = ()
    results = ()
    n = operation[1] if len(operation) > 1 else 1
    if kind in ("alloc", "both"):
      assert 1 <= n <= LANES
      if kind == "both":
        addresses = tuple(operation[2])
        assert 1 <= len(addresses) <= LANES
        assert len(set(addresses)) == len(addresses)
        assert set(addresses) <= oracle.live
      available = capacity - oracle.bump + len(oracle.free)
      if n > available:
        states.append(oracle.advance(done=True, oom=True))
      else:
        reused = min(n, len(oracle.free))
        bumped = n - reused
        results = tuple(reversed(oracle.free[-reused:])) if reused else ()
        results += tuple(range(oracle.bump, oracle.bump + bumped))
        if not reused:
          states.append(oracle.advance(bump=bumped, done=True))
        for index in range(reused):
          for _ in range(heap_delay):
            states.append(oracle.advance())
          last = index == reused - 1
          states.append(oracle.advance(
            recycle=True, bump=bumped if last else 0, done=last,
          ))
    else:
      addresses = (n,) if isinstance(n, int) else tuple(n)
      assert 1 <= len(addresses) <= LANES
      assert len(set(addresses)) == len(addresses), "duplicate free in script"
      assert set(addresses) <= oracle.live, "scripts must only free live addresses"
      for index, addr in enumerate(reversed(addresses)):
        for _ in range(heap_delay):
          states.append(oracle.advance())
        states.append(oracle.advance(free_addr=addr, done=index == len(addresses) - 1))
    schedule.append({
      "kind": kind, "start": start, "finish": len(states) - 1,
      "count": n if kind != "free" else 0,
      "addresses": addresses, "results": results,
    })
  # Include the clearing of done/oom and the lagged peak update.
  states.extend((oracle.advance(), oracle.advance()))
  return schedule, states


def assert_addresses(m, dut, expected):
  """The first n lanes must be a permutation, not a prescribed ordering."""
  for index in range(len(expected)):
    addr = dut.alloc_addrs[index]
    m.d.comb += Assert(Cat(*(addr == value for value in expected)).any())
    for previous in range(index):
      m.d.comb += Assert(addr != dut.alloc_addrs[previous])


def verify_sequence(name, operations, capacity=4, heap_delay=1):
  schedule, states = schedule_sequence(operations, capacity, heap_delay)
  dut, verify = setup(capacity=capacity, cycles=len(states) - 1)
  m, cycle = verify.m, verify.cycle
  words, reads, writes = add_heap_model(m, dut, heap_delay)
  # Exclude unreachable spare encodings through an asserted harness invariant,
  # so induction cannot park outside every scripted cycle's assertions.
  m.d.comb += Assert(cycle < len(states))
  for step in schedule:
    with m.If(cycle == step["start"]):
      if step["kind"] in ("alloc", "both"):
        m.d.comb += dut.req_alloc.eq(step["count"])
      if step["kind"] in ("free", "both"):
        padded = step["addresses"] + (0,) * (LANES - len(step["addresses"]))
        m.d.comb += [
          dut.req_free.eq(len(step["addresses"])),
          dut.free_addrs.eq(Const(padded, dut.free_addrs.shape())),
        ]
    with m.If((cycle > step["start"]) & (cycle < step["finish"])):
      # Counts/data must have been captured, not consumed live while busy.
      m.d.comb += [
        dut.req_alloc.eq(LANES), dut.req_free.eq(LANES),
        dut.free_addrs.eq(Const([EMPTY] * LANES, dut.free_addrs.shape())),
      ]
    if step["results"]:
      with m.If(cycle == step["finish"]):
        assert_addresses(m, dut, step["results"])
    m.d.comb += Cover((cycle == step["finish"]) & dut.done)

  for tick, expected in enumerate(states):
    with m.If(cycle == tick):
      m.d.comb += [
        Assert(dut.done == expected["done"]), Assert(dut.oom == expected["oom"]),
        Assert(dut.empty == expected["bump"]), Assert(dut.free_head == expected["head"]),
        Assert(dut.free_count == expected["free_count"]),
        Assert(dut.stat_allocs == expected["allocs"]),
        Assert(dut.stat_frees == expected["frees"]), Assert(dut.stat_peak == expected["peak"]),
        Assert(reads == expected["reads"]), Assert(writes == expected["writes"]),
      ]
      for addr, value in enumerate(expected["words"]):
        m.d.comb += Assert(words[addr] == value)
      if tick == len(states) - 1:
        m.d.comb += [Assert(~dut.heap.read), Assert(~dut.heap.write)]
  m.d.comb += Cover(cycle == len(states) - 1)
  verify.prove(name, depth=max(6, len(states) + 1))


def verify_idle():
  verify_sequence("verify_idle", [("idle", 4)])


def verify_one_bump_alloc():
  verify_sequence("verify_one_bump_alloc", [("alloc", 1)])


def verify_two_bump_allocs():
  verify_sequence("verify_two_bump_allocs", [("alloc", 1), ("idle", 2), ("alloc", 1)])


def verify_consecutive_bump_allocs():
  verify_sequence("verify_consecutive_bump_allocs", [("alloc", 1)] * 4)


def verify_capacity_boundaries():
  for capacity in (1, 3, 4):
    verify_sequence(f"verify_capacity_{capacity}", [("alloc", 1)] * (capacity + 2),
                    capacity=capacity)


def verify_repeated_oom():
  verify_sequence("verify_repeated_oom", [("alloc", 4)] + [("alloc", 1)] * 3)


def verify_free_waits_and_captures_address():
  verify_sequence("verify_free_waits_and_captures_address",
                  [("alloc", 3), ("free", 2)], heap_delay=3)


def verify_realloc_then_bump():
  verify_sequence("verify_realloc_then_bump",
                  [("alloc", 3), ("free", 0), ("alloc", 1), ("alloc", 1)])


def verify_free_list_lifo():
  verify_sequence("verify_free_list_lifo",
                  [("alloc", 3), ("free", 0), ("free", 2)] + [("alloc", 1)] * 3)


def verify_delayed_reuse():
  verify_sequence("verify_delayed_reuse",
                  [("alloc", 2), ("free", 1), ("free", 0)] + [("alloc", 1)] * 3,
                  heap_delay=3)


def verify_oom_recovery():
  verify_sequence("verify_oom_recovery",
                  [("alloc", 4), ("alloc", 1), ("free", 1), ("alloc", 1), ("alloc", 1)])


def verify_free_all_and_reallocate():
  verify_sequence("verify_free_all_and_reallocate",
                  [("alloc", 4), ("free", (0, 1, 2, 3)), ("alloc", 4)])


def verify_repeated_reuse():
  verify_sequence("verify_repeated_reuse",
                  [("alloc", 1)] + [("free", 0), ("alloc", 1)] * 3 + [("alloc", 1)])


def verify_allocation_priority():
  verify_sequence("verify_allocation_priority", [
    ("alloc", 1), ("both", 2, (0,)), ("both", 1, (0,)), ("both", 1, (0,)),
  ])


def verify_vector_bump():
  for count in range(1, LANES + 1):
    verify_sequence(f"verify_vector_bump_{count}", [("alloc", count)], capacity=count)


def verify_vector_free_captures_inputs():
  verify_sequence("verify_vector_free_captures_inputs",
                  [("alloc", 4), ("free", (1, 3, 0))], heap_delay=3)


def verify_vector_reuse():
  verify_sequence("verify_vector_reuse",
                  [("alloc", 4), ("free", (0, 1, 2, 3)), ("alloc", 2)], capacity=4)


def verify_vector_mixed_allocation():
  verify_sequence("verify_vector_mixed_allocation",
                  [("alloc", 3), ("free", 0), ("alloc", 2)], capacity=4)


def verify_vector_mixed_multiple_frees():
  verify_sequence("verify_vector_mixed_multiple_frees",
                  [("alloc", 4), ("free", (0, 2)), ("alloc", 4)], capacity=6, heap_delay=3)


def verify_vector_oom_is_atomic():
  verify_sequence("verify_vector_oom_is_atomic", [
    ("alloc", 4), ("free", (0, 2)), ("alloc", 3), ("alloc", 1),
  ])


def verify_vector_partial_capacity():
  verify_sequence("verify_vector_partial_capacity",
                  [("alloc", 4), ("alloc", 4), ("alloc", 3), ("alloc", 1)], capacity=7)


def verify_vector_repeated_batches():
  verify_sequence("verify_vector_repeated_batches", [
    ("alloc", 4), ("free", (3, 1, 0, 2)), ("alloc", 4),
    ("free", (0, 1, 2, 3)), ("alloc", 4),
  ])


def verify_arbitrary_bump_requests():
  dut = Alloc(capacity=4)
  fragment = Fragment.get(dut, platform=None)
  fsm = fragment.generated["fsm"]
  verify = Verify(fragment, cycles=7)
  m = verify.m
  request = AnySeq(len(dut.req_alloc))
  expected_empty = Signal(range(dut.capacity + 1))
  expected_done, expected_oom = Signal(), Signal()
  expected_base = Signal(Addr)
  expected_count = Signal(range(LANES + 1))
  expected_peak = Signal(Addr)
  m.d.comb += [
    Assume(request <= LANES),  # Restrict the caller to representable valid counts.
    dut.req_alloc.eq(request), dut.req_free.eq(0), dut.free_addrs.eq(0),
    dut.stat_reset.eq(False), dut.heap.done.eq(False), dut.heap.read_data.eq(0),
    Assert(fsm.ongoing(IDLE)),
  ]
  m.d.sync += [
    expected_done.eq(request != 0), expected_oom.eq(False),
    expected_peak.eq(Mux(expected_empty > expected_peak, expected_empty, expected_peak)),
  ]
  with m.If(request != 0):
    with m.If(request <= dut.capacity - expected_empty):
      m.d.sync += [
        expected_base.eq(expected_empty), expected_count.eq(request),
        expected_empty.eq(expected_empty + request),
      ]
    with m.Else():
      m.d.sync += expected_oom.eq(True)
  m.d.comb += [
    Assert(dut.empty == expected_empty), Assert(dut.empty <= dut.capacity),
    Assert(dut.done == expected_done), Assert(dut.oom == expected_oom),
    Assert(dut.free_head == ADDR_EMPTY), Assert(dut.free_count == 0),
    Assert(dut.stat_allocs == expected_empty), Assert(dut.stat_frees == 0),
    Assert(dut.stat_peak == expected_peak),
    Assert(~dut.heap.read), Assert(~dut.heap.write),
    Cover(dut.done & dut.oom), Cover(~dut.done),
  ]
  for lane in range(LANES):
    with m.If(expected_done & ~expected_oom & (lane < expected_count)):
      m.d.comb += [
        Assert(dut.alloc_addrs[lane] >= expected_base),
        Assert(dut.alloc_addrs[lane] < expected_base + expected_count),
      ]
      for previous in range(lane):
        m.d.comb += Assert(dut.alloc_addrs[lane] != dut.alloc_addrs[previous])
    m.d.comb += Cover(dut.done & ~dut.oom & (expected_count == lane + 1))
  verify.prove(depth=8)


if __name__ == "__main__":
  tests = [
    verify_idle, verify_one_bump_alloc, verify_two_bump_allocs,
    verify_consecutive_bump_allocs, verify_capacity_boundaries, verify_repeated_oom,
    verify_free_waits_and_captures_address, verify_realloc_then_bump,
    verify_free_list_lifo, verify_delayed_reuse, verify_oom_recovery,
    verify_free_all_and_reallocate, verify_repeated_reuse, verify_allocation_priority,
    verify_vector_bump, verify_vector_free_captures_inputs, verify_vector_reuse,
    verify_vector_mixed_allocation, verify_vector_mixed_multiple_frees,
    verify_vector_oom_is_atomic, verify_vector_partial_capacity,
    verify_vector_repeated_batches, verify_arbitrary_bump_requests,
  ]
  failures = []
  for test in tests:
    try:
      test()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
      failures.append(test.__name__)
      print(f"FAIL {test.__name__}: {error}", flush=True)
  print(f"Allocator test groups: {len(tests) - len(failures)} passed, {len(failures)} failed")
  if failures:
    print("Failed groups: " + ", ".join(failures))
  raise SystemExit(bool(failures))
