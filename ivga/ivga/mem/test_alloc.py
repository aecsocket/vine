"""Allocator proofs: run `python -m ivga.mem.test_alloc`.

Scripted tests check timing and free-list behavior against a Python oracle.
The symbolic bump test checks every allocation-request sequence with no frees.
The heap model acknowledges each transaction once and poisons inactive reads.
Invalid frees/double frees are caller errors, not supported allocator behavior.
"""

from amaranth import Array, Assert, Cover, Mux, Signal
from amaranth.asserts import AnySeq
from amaranth.hdl import Fragment

from ivga import Word
from ivga.mem.alloc import ADDR_EMPTY, IDLE, Alloc
from ivga.verify import Verify

EMPTY = ADDR_EMPTY.value
POISON = 0xDEAD_BEEF


def setup(capacity=4, cycles=5):
  dut = Alloc(capacity=capacity)
  verify = Verify(dut, cycles=cycles)
  verify.m.d.comb += [
    dut.req_alloc.eq(False),
    dut.req_free.eq(False),
    dut.free_addr.eq(0),
    dut.heap.done.eq(0),
    dut.heap.read_data.eq(0),
  ]
  return dut, verify


def assert_bump_allocd(m, dut: Alloc, at: int):
  m.d.comb += [
    Assert(dut.done),
    Assert(~dut.oom),
    Assert(dut.empty == at + 1),
    Assert(dut.alloc_addr == at),
  ]


def add_heap_model(m, dut: Alloc, delay: int):
  """One transaction completes `delay` edges after its initial capture.

  Requests must stay stable through acknowledgement. The allocator's initial
  request state plus wait state give a total latency of delay + 1 edges.
  """
  assert delay >= 1
  words = Array(Signal(Word, name=f"heap_{i}") for i in range(dut.capacity))
  pending = Signal()
  remaining = Signal(range(delay))
  read = Signal()
  write = Signal()
  addr = Signal.like(dut.heap.addr)
  data = Signal(Word)
  reads = Signal(16)
  writes = Signal(16)
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
      pending.eq(True),
      remaining.eq(delay - 1),
      read.eq(dut.heap.read),
      write.eq(dut.heap.write),
      addr.eq(dut.heap.addr),
      data.eq(dut.heap.write_data),
    ]
  with m.Elif(pending):
    # These assert client behavior, rather than assuming the DUT is correct.
    m.d.comb += [
      Assert(dut.heap.read == read),
      Assert(dut.heap.write == write),
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


def verify_sequence(name, operations, capacity=4, heap_delay=1):
  """Build a legal operation script and independently predict its results."""
  bump = 0
  free = []
  live = set()
  schedule = []
  cycle = 0
  read_count = 0
  write_count = 0

  for operation in operations:
    kind = operation[0]
    before_bump = bump
    before_head = free[-1] if free else EMPTY
    oom = False
    addr = None
    if kind == "alloc":
      if free:
        addr = free.pop()
        latency = heap_delay + 1
        read_count += 1
      elif bump < capacity:
        addr = bump
        bump += 1
        latency = 1
      else:
        oom = True
        latency = 1
      if addr is not None:
        assert addr not in live
        live.add(addr)
    else:
      assert kind == "free"
      addr = operation[1]
      assert addr in live, "scripts must only free live addresses"
      live.remove(addr)
      free.append(addr)
      latency = heap_delay + 1
      write_count += 1

    finish = cycle + latency
    schedule.append({
      "kind": kind, "start": cycle, "finish": finish, "addr": addr, "oom": oom,
      "before_bump": before_bump, "before_head": before_head, "bump": bump,
      "head": free[-1] if free else EMPTY,
    })
    cycle = finish

  # Leave a distinct final idle cycle before the harness counter saturates.
  dut, verify = setup(capacity=capacity, cycles=cycle + 2)
  m, tick = verify.m, verify.cycle
  words, reads, writes = add_heap_model(m, dut, heap_delay)

  for step in schedule:
    with m.If(tick == step["start"]):
      if step["kind"] == "alloc":
        m.d.comb += dut.req_alloc.eq(True)
      else:
        m.d.comb += [dut.req_free.eq(True), dut.free_addr.eq(step["addr"])]

    with m.If((tick > step["start"]) & (tick < step["finish"])):
      m.d.comb += [
        Assert(~dut.done),
        Assert(~dut.oom),
        Assert(dut.empty == step["before_bump"]),
        Assert(dut.free_head == step["before_head"]),
      ]
      if step["kind"] == "free":
        # Change the caller's address while busy: the captured address must win.
        m.d.comb += dut.free_addr.eq((step["addr"] + 1) % capacity)

    with m.If(tick == step["finish"]):
      m.d.comb += [
        Assert(dut.done),
        Assert(dut.oom == step["oom"]),
        Assert(dut.empty == step["bump"]),
        Assert(dut.free_head == step["head"]),
      ]
      if step["kind"] == "alloc" and not step["oom"]:
        m.d.comb += Assert(dut.alloc_addr == step["addr"])
      elif step["kind"] == "free":
        m.d.comb += Assert(words[step["addr"]] == step["before_head"])
    m.d.comb += Cover((tick == step["finish"]) & dut.done)

  with m.If(tick > cycle):
    m.d.comb += [
      Assert(~dut.done),
      Assert(~dut.oom),
      Assert(~dut.heap.read),
      Assert(~dut.heap.write),
      Assert(dut.empty == bump),
      Assert(dut.free_head == (free[-1] if free else EMPTY)),
      Assert(reads == read_count),
      Assert(writes == write_count),
    ]
  m.d.comb += Cover(tick == cycle + 1)
  verify.prove(name, depth=max(6, cycle + 3))


def verify_idle():
  dut, verify = setup()
  m, cycle = verify.m, verify.cycle
  m.d.comb += [
    Assert(dut.empty == 0),
    Assert(dut.free_head == ADDR_EMPTY),
    Assert(~dut.done),
    Assert(~dut.oom),
    Assert(~dut.heap.read),
    Assert(~dut.heap.write),
    Cover(cycle == 4),
  ]
  verify.prove()


def verify_one_bump_alloc():
  dut, verify = setup()
  m, cycle = verify.m, verify.cycle
  with m.If(cycle == 0):
    m.d.comb += [
      Assert(dut.empty == 0),
      Assert(~dut.done),
      dut.req_alloc.eq(True),
    ]
  with m.If(cycle == 1):
    assert_bump_allocd(m, dut, at=0)
  with m.If(cycle >= 2):
    m.d.comb += [Assert(~dut.done), Assert(dut.empty == 1)]
  m.d.comb += Cover((cycle == 1) & dut.done)
  verify.prove()


def verify_two_bump_allocs():
  dut, verify = setup()
  m, cycle = verify.m, verify.cycle
  with m.If((cycle == 0) | (cycle == 3)):
    m.d.comb += dut.req_alloc.eq(True)
  with m.If(cycle == 1):
    assert_bump_allocd(m, dut, at=0)
  with m.If((cycle == 2) | (cycle == 3)):
    m.d.comb += [Assert(~dut.done), Assert(dut.empty == 1)]
  with m.If(cycle == 4):
    assert_bump_allocd(m, dut, at=1)
  m.d.comb += [
    Cover((cycle == 1) & dut.done),
    Cover((cycle == 4) & dut.done),
  ]
  verify.prove()


def verify_consecutive_bump_allocs():
  verify_sequence("verify_consecutive_bump_allocs", [("alloc",)] * 4)


def verify_capacity_boundaries():
  for capacity in (1, 3, 4):
    verify_sequence(f"verify_capacity_{capacity}", [("alloc",)] * (capacity + 2),
                    capacity=capacity)


def verify_repeated_oom():
  verify_sequence("verify_repeated_oom", [("alloc",)] * 7)


def verify_free_waits_and_captures_address():
  verify_sequence("verify_free_waits_and_captures_address",
                  [("alloc",)] * 3 + [("free", 2)], heap_delay=3)


def verify_realloc_then_bump():
  verify_sequence("verify_realloc_then_bump",
                  [("alloc",), ("free", 0), ("alloc",), ("alloc",)])


def verify_free_list_lifo():
  # Returned addresses after the frees must be 2, 0, then bump address 3.
  verify_sequence("verify_free_list_lifo",
                  [("alloc",)] * 3 + [("free", 0), ("free", 2)] + [("alloc",)] * 3)


def verify_delayed_reuse():
  verify_sequence("verify_delayed_reuse",
                  [("alloc",)] * 2 + [("free", 1), ("free", 0)] + [("alloc",)] * 3,
                  heap_delay=3)


def verify_oom_recovery():
  verify_sequence("verify_oom_recovery",
                  [("alloc",)] * 5 + [("free", 1), ("alloc",), ("alloc",)])


def verify_free_all_and_reallocate():
  verify_sequence("verify_free_all_and_reallocate",
                  [("alloc",)] * 4 + [("free", i) for i in range(4)] + [("alloc",)] * 5)


def verify_repeated_reuse():
  verify_sequence("verify_repeated_reuse",
                  [("alloc",)] + [("free", 0), ("alloc",)] * 3 + [("alloc",)])


def verify_allocation_priority():
  # The current interface explicitly gives alloc priority over simultaneous free.
  dut, verify = setup()
  m, cycle = verify.m, verify.cycle
  with m.If(cycle < 2):
    m.d.comb += [dut.req_alloc.eq(True), dut.req_free.eq(True)]
  with m.If(cycle == 1):
    assert_bump_allocd(m, dut, at=0)
  with m.If(cycle == 2):
    assert_bump_allocd(m, dut, at=1)
  m.d.comb += [
    Assert(dut.free_head == ADDR_EMPTY),
    Assert(~dut.heap.write),
    Cover((cycle == 2) & dut.done),
  ]
  verify.prove()


def verify_arbitrary_bump_requests():
  # Reuse this exact fragment so the invariant refers to the DUT's real FSM.
  dut = Alloc(capacity=4)
  fragment = Fragment.get(dut, platform=None)
  fsm = fragment.generated["fsm"]
  verify = Verify(fragment, cycles=7)
  m = verify.m
  m.d.comb += [
    dut.req_free.eq(False),
    dut.free_addr.eq(0),
    dut.heap.done.eq(0),
    dut.heap.read_data.eq(0),
    # An asserted inductive invariant, not an assumption about reachable states.
    Assert(fsm.ongoing(IDLE)),
  ]
  request = AnySeq(1)
  expected_empty = Signal(range(dut.capacity + 1))
  expected_done = Signal()
  expected_oom = Signal()
  expected_addr = Signal.like(dut.alloc_addr)
  m.d.comb += dut.req_alloc.eq(request)
  m.d.sync += [expected_done.eq(request), expected_oom.eq(False)]
  with m.If(request):
    with m.If(expected_empty < dut.capacity):
      m.d.sync += [
        expected_addr.eq(expected_empty),
        expected_empty.eq(expected_empty + 1),
      ]
    with m.Else():
      m.d.sync += expected_oom.eq(True)

  m.d.comb += [
    Assert(dut.empty == expected_empty),
    Assert(dut.empty <= dut.capacity),
    Assert(dut.done == expected_done),
    Assert(dut.oom == expected_oom),
    Assert(dut.free_head == ADDR_EMPTY),
    Assert(~dut.heap.read),
    Assert(~dut.heap.write),
    Cover(dut.done & ~dut.oom),
    Cover(dut.done & dut.oom),
  ]
  with m.If(expected_done & ~expected_oom):
    m.d.comb += Assert(dut.alloc_addr == expected_addr)
  verify.prove(depth=8)


if __name__ == "__main__":
  tests = [
    verify_idle,
    verify_one_bump_alloc,
    verify_two_bump_allocs,
    verify_consecutive_bump_allocs,
    verify_capacity_boundaries,
    verify_repeated_oom,
    verify_free_waits_and_captures_address,
    verify_realloc_then_bump,
    verify_free_list_lifo,
    verify_delayed_reuse,
    verify_oom_recovery,
    verify_free_all_and_reallocate,
    verify_repeated_reuse,
    verify_allocation_priority,
    verify_arbitrary_bump_requests,
  ]
  for test in tests:
    test()
