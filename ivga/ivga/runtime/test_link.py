"""Small link simulations: run `python -m ivga.runtime.test_link`.

The Python heap/allocator mocks execute held requests exactly once. Delayed
responses model waiting for arbitration, not a real shared-memory arbiter.
"""

from amaranth import Const
from amaranth.sim import Simulator

from ivga import Port, Tag
from ivga.mem.alloc import ADDR_EMPTY
from ivga.runtime import Link

PERIOD = 1e-9


def port(tag, payload=0):
  return Const({"tag": tag, "payload": payload, "label": 0}, Port).as_value().value


def wire(addr):
  return port(Tag.WIRE, addr)


ERASE = port(Tag.ERASE)
POISON = port(Tag.EXT_FN)


def check_links(links, initial, expected, expected_freed, expected_heap_ops,
                expected_heap_writes=None):
  failures = []
  for delay in (1, 3):
    try:
      simulate_links(links, initial, expected, expected_freed, expected_heap_ops,
                     delay, expected_heap_writes)
    except AssertionError as error:
      failures.append(f"delay={delay}: {error}")
  assert not failures, "; ".join(failures)


def simulate_links(links, initial, expected, expected_freed, expected_heap_ops,
                   delay, expected_heap_writes=None):
  dut = Link()
  sim = Simulator(dut)
  sim.add_clock(PERIOD)
  heap = dict(initial)
  freed = []
  heap_ops = []

  def heap_request(ctx):
    return (ctx.get(dut.heap.read), ctx.get(dut.heap.write),
            ctx.get(dut.heap.addr), ctx.get(dut.heap.write_data))

  async def heap_service(ctx):
    while True:
      read, write, addr, data = request = heap_request(ctx)
      if not (read or write):
        await ctx.tick()
        continue

      for _ in range(delay):
        await ctx.tick()
        assert heap_request(ctx) == request, "heap request changed while waiting"

      # Read-first swap: return the old word, not the replacement.
      old = heap.get(addr, 0)
      if write:
        heap[addr] = data
      heap_ops.append(request)
      ctx.set(dut.heap.read_data, old if read else POISON)
      ctx.set(dut.heap.done, True)
      assert heap_request(ctx) == request, "heap request changed during acknowledgement"
      await ctx.tick()
      ctx.set(dut.heap.done, False)
      ctx.set(dut.heap.read_data, POISON)

  async def alloc_service(ctx):
    while True:
      assert not ctx.get(dut.alloc.req_alloc), "link should only free slots"
      if not ctx.get(dut.alloc.req_free):
        await ctx.tick()
        continue

      addr = ctx.get(dut.alloc.free_addr)
      for _ in range(delay):
        await ctx.tick()
        assert ctx.get(dut.alloc.req_free), "free request dropped early"
        assert ctx.get(dut.alloc.free_addr) == addr, "free address changed while waiting"

      assert addr not in freed, f"double free of {addr}"
      # Freeing overwrites the old port with free-list metadata.
      heap[addr] = freed[-1] if freed else ADDR_EMPTY.value
      freed.append(addr)
      ctx.set(dut.alloc.done, True)
      assert ctx.get(dut.alloc.free_addr) == addr, "free address changed during acknowledgement"
      await ctx.tick()
      ctx.set(dut.alloc.done, False)

  async def bench(ctx):
    assert not ctx.get(dut.done)
    for a, b in links:
      ctx.set(dut.req_a.as_value(), a)
      ctx.set(dut.req_b.as_value(), b)
      ctx.set(dut.req_link, True)
      await ctx.tick()
      ctx.set(dut.req_link, False)
      # Verify the operands were captured instead of being used live.
      ctx.set(dut.req_a.as_value(), POISON)
      ctx.set(dut.req_b.as_value(), POISON)

      for _ in range(100):
        if ctx.get(dut.done):
          break
        await ctx.tick()
      else:
        raise AssertionError("link did not complete within 100 cycles")

      await ctx.tick()
      assert not ctx.get(dut.done), "done must clear after completion"
      assert not ctx.get(dut.heap.read)
      assert not ctx.get(dut.heap.write)
      assert not ctx.get(dut.alloc.req_free)

    assert heap == expected, (heap, expected)
    assert freed == expected_freed, (freed, expected_freed)
    assert len(heap_ops) == expected_heap_ops, heap_ops
    if expected_heap_writes is not None:
      assert sum(write for _, write, _, _ in heap_ops) == expected_heap_writes, heap_ops

  sim.add_testbench(heap_service, background=True)
  sim.add_testbench(alloc_service, background=True)
  sim.add_testbench(bench)
  sim.run()


def test_erase_pair():
  check_links([(ERASE, ERASE)], {}, {}, [], 0)


def test_connect_empty_wire():
  check_links([(wire(1), ERASE)], {}, {1: ERASE}, [], 1)


def test_connect_reversed_operands():
  check_links([(ERASE, wire(2))], {}, {2: ERASE}, [], 1)


def test_connect_occupied_wire():
  check_links([(wire(1), ERASE)], {1: ERASE}, {1: ADDR_EMPTY.value}, [1], 1)


def test_follow_wire_chain():
  check_links([(wire(1), wire(2))], {2: wire(3), 3: ERASE},
              {1: ERASE, 2: ADDR_EMPTY.value, 3: 2}, [2, 3], 3)


def test_two_links_rendezvous():
  check_links([(wire(1), ERASE), (wire(1), ERASE)], {},
              {1: ADDR_EMPTY.value}, [1], 2)


def test_connect_two_empty_wires():
  # Read empty B, then start a separate swap at A; neither slot is freed.
  check_links([(wire(1), wire(2))], {}, {1: wire(2)}, [], 2,
              expected_heap_writes=1)


def test_connect_occupied_wire_to_empty_wire():
  # After following empty B, move A's saved endpoint through B and reclaim A.
  check_links([(wire(1), wire(2))], {1: ERASE},
              {1: ADDR_EMPTY.value, 2: ERASE}, [1], 3,
              expected_heap_writes=2)


def test_follow_chain_to_empty_wire():
  # Reclaim the occupied intermediate slot, but retain the empty tail.
  check_links([(wire(1), wire(2))], {2: wire(3)},
              {1: wire(3), 2: ADDR_EMPTY.value}, [2], 3,
              expected_heap_writes=1)


def test_connect_same_wire():
  # Cancel the two ends: free once, never store a self-reference.
  check_links([(wire(1), wire(1))], {}, {1: ADDR_EMPTY.value}, [1], 1,
              expected_heap_writes=0)


def test_follow_chain_back_to_anchor():
  # Following B resolves to A, so reclaim B and then A without a swap.
  check_links([(wire(1), wire(2))], {2: wire(1)},
              {1: 2, 2: ADDR_EMPTY.value}, [2, 1], 2,
              expected_heap_writes=0)


def test_follow_long_chain_back_to_anchor():
  check_links([(wire(1), wire(2))], {2: wire(3), 3: wire(1)},
              {1: 3, 2: ADDR_EMPTY.value, 3: 2}, [2, 3, 1], 3,
              expected_heap_writes=0)


if __name__ == "__main__":
  tests = [
    test_erase_pair,
    test_connect_empty_wire,
    test_connect_reversed_operands,
    test_connect_occupied_wire,
    test_follow_wire_chain,
    test_two_links_rendezvous,
    test_connect_two_empty_wires,
    test_connect_occupied_wire_to_empty_wire,
    test_follow_chain_to_empty_wire,
    test_connect_same_wire,
    test_follow_chain_back_to_anchor,
    test_follow_long_chain_back_to_anchor,
  ]
  failures = []
  for test in tests:
    try:
      test()
    except AssertionError as error:
      failures.append(test.__name__)
      print(f"FAIL {test.__name__}: {error}", flush=True)
    else:
      print(f"PASS {test.__name__}", flush=True)
  assert not failures, f"{len(failures)} link cases failed: {', '.join(failures)}"
  print(f"Passed {len(tests) * 2} link simulations ({len(tests)} cases, 2 response delays)")
