"""Link simulations: run `python -m ivga.runtime.test_link`.

Heap/allocator mocks execute held requests once, with delayed responses.
The worklist is a real Stack with backing Memory. A test-only supervisor
latches overflow events and feeds halt back; the caller stops submitting work.
"""

from amaranth import Const, Module, Signal
from amaranth.sim import Simulator

from ivga import Port, Port2, Tag
from ivga.mem import Stack
from ivga.mem.alloc import ADDR_EMPTY
from ivga.runtime import Link

PERIOD = 1e-9
PORT_BITS = Port.as_shape().size


def port(tag, payload=0, label=0):
  return Const({"tag": tag, "payload": payload, "label": label}, Port).as_value().value


def wire(addr):
  return port(Tag.WIRE, addr)


def pair_bits(a, b):
  return a | (b << PORT_BITS)


ERASE = port(Tag.ERASE)
POISON = port(Tag.EXT_FN)


def check_links(links, initial, expected, expected_freed, expected_heap_ops,
                expected_heap_writes=None, *, expected_active=(), worklist_depth=4,
                expect_overflow=False):
  failures = []
  for delay in (1, 3):
    try:
      simulate_links(links, initial, expected, expected_freed, expected_heap_ops,
                     delay, expected_heap_writes, expected_active=expected_active,
                     worklist_depth=worklist_depth, expect_overflow=expect_overflow)
    except AssertionError as error:
      failures.append(f"delay={delay}: {error}")
  assert not failures, "; ".join(failures)


def simulate_links(links, initial, expected, expected_freed, expected_heap_ops,
                   delay, expected_heap_writes=None, *, expected_active=(),
                   worklist_depth=4, expect_overflow=False):
  dut = Link()
  active = Stack(Port2, depth=worklist_depth)
  m = Module()
  m.submodules.link = dut
  m.submodules.active = active
  pop_request = Signal()
  overflow_latched = Signal()
  m.d.comb += [
    active.req_push.eq(dut.active.req_push),
    active.push_data.eq(dut.active.push_data),
    active.req_pop.eq(pop_request),
    dut.active.full.eq(active.full),
    dut.active.empty.eq(active.empty),
    dut.active.done.eq(active.done),
    dut.active.pop_data.eq(active.pop_data),
    dut.fault.halt.eq(overflow_latched),
  ]
  # This verifies event capture with a test supervisor, not a production one.
  m.d.sync += overflow_latched.eq(overflow_latched | dut.fault.active_full)

  sim = Simulator(m)
  sim.add_clock(PERIOD)
  heap = dict(initial)
  freed = []
  heap_ops = []
  pushed = []
  fault_events = []

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
    async def step():
      # Sample before register updates: fast-path pushes can last only one cycle.
      _, _, push, full, data, overflow, oom, pop = await ctx.tick().sample(
        dut.active.req_push, active.full, dut.active.push_data.as_value(),
        dut.fault.active_full, dut.fault.oom, dut.active.req_pop,
      )
      assert not pop, "link should not consume worklist entries"
      assert not oom, "link should not report allocator OOM"
      if push:
        assert not full, "push attempted while worklist was full"
        assert not overflow, "overflow must not also push an entry"
        pushed.append(data)
      if overflow:
        fault_events.append(True)

    assert not ctx.get(dut.done)
    for index, (a, b) in enumerate(links):
      ctx.set(dut.req_a.as_value(), a)
      ctx.set(dut.req_b.as_value(), b)
      ctx.set(dut.req_link, True)
      await step()
      ctx.set(dut.req_link, False)
      # Verify the operands were captured instead of being used live.
      ctx.set(dut.req_a.as_value(), POISON)
      ctx.set(dut.req_b.as_value(), POISON)

      for _ in range(100):
        if ctx.get(dut.done) or ctx.get(overflow_latched):
          break
        await step()
      else:
        raise AssertionError("link did not complete or fault within 100 cycles")

      should_fault = expect_overflow and index == len(links) - 1
      assert bool(ctx.get(overflow_latched)) == should_fault, "unexpected overflow outcome"
      if should_fault:
        assert not ctx.get(dut.done), "overflow was incorrectly reported as success"
      else:
        assert ctx.get(dut.done)

      await step()
      assert not ctx.get(dut.done), "done must clear after completion"
      assert not ctx.get(dut.heap.read)
      assert not ctx.get(dut.heap.write)
      assert not ctx.get(dut.alloc.req_free)
      assert not ctx.get(dut.active.req_push), "worklist push was replayed"
      assert not ctx.get(dut.fault.active_full), "overflow event did not clear with the request"

    expected_packed = [pair_bits(a, b) for a, b in expected_active]
    assert pushed == expected_packed, (pushed, expected_packed)
    assert ctx.get(active.top) == len(expected_active), "wrong number of stored pairs"
    assert len(fault_events) == int(expect_overflow), "unexpected/repeated fault event"
    assert heap == expected, (heap, expected)
    assert freed == expected_freed, (freed, expected_freed)
    assert len(heap_ops) == expected_heap_ops, heap_ops
    if expected_heap_writes is not None:
      assert sum(write for _, write, _, _ in heap_ops) == expected_heap_writes, heap_ops

    # Read the actual RAM-backed stack, not just the observed push bus.
    for expected_pair in reversed(expected_packed):
      ctx.set(pop_request, True)
      await step()
      assert ctx.get(active.done)
      assert ctx.get(active.pop_data.as_value()) == expected_pair, "stored active pair corrupted"
    ctx.set(pop_request, False)
    await step()
    assert ctx.get(active.empty)
    assert bool(ctx.get(overflow_latched)) == expect_overflow
    assert bool(ctx.get(dut.fault.halt)) == expect_overflow

    if expect_overflow:
      # Making room must not clear a sticky fault or resurrect the lost pair.
      for _ in range(3):
        await step()
        assert ctx.get(overflow_latched)
        assert not ctx.get(dut.fault.active_full)
        assert not ctx.get(dut.active.req_push)
        assert not ctx.get(dut.done)


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
  check_links([(wire(1), wire(2))], {}, {1: wire(2)}, [], 2,
              expected_heap_writes=1)


def test_connect_occupied_wire_to_empty_wire():
  check_links([(wire(1), wire(2))], {1: ERASE},
              {1: ADDR_EMPTY.value, 2: ERASE}, [1], 3,
              expected_heap_writes=2)


def test_follow_chain_to_empty_wire():
  check_links([(wire(1), wire(2))], {2: wire(3)},
              {1: wire(3), 2: ADDR_EMPTY.value}, [2], 3,
              expected_heap_writes=1)


def test_connect_same_wire():
  check_links([(wire(1), wire(1))], {}, {1: ADDR_EMPTY.value}, [1], 1,
              expected_heap_writes=0)


def test_follow_chain_back_to_anchor():
  check_links([(wire(1), wire(2))], {2: wire(1)},
              {1: 2, 2: ADDR_EMPTY.value}, [2, 1], 2,
              expected_heap_writes=0)


def test_follow_long_chain_back_to_anchor():
  check_links([(wire(1), wire(2))], {2: wire(3), 3: wire(1)},
              {1: 3, 2: ADDR_EMPTY.value, 3: 2}, [2, 3, 1], 3,
              expected_heap_writes=0)


def test_nonwire_dispatch_matrix():
  tags = (Tag.GRAFT, Tag.ERASE, Tag.EXT_VAL, Tag.COMB, Tag.EXT_FN)
  graft_erase = {Tag.GRAFT, Tag.ERASE}
  value_erase = {Tag.EXT_VAL, Tag.ERASE}
  links = []
  queued = []
  for a_tag in tags:
    for b_tag in tags:
      a = ERASE if a_tag == Tag.ERASE else port(a_tag, 17, 0 if a_tag == Tag.GRAFT else 3)
      b = ERASE if b_tag == Tag.ERASE else port(b_tag, 29, 0 if b_tag == Tag.GRAFT else 5)
      pair = (a, b)
      links.append(pair)
      cancels = ((a_tag in graft_erase and b_tag in graft_erase)
                 or (a_tag in value_erase and b_tag in value_erase))
      if not cancels:
        queued.append(pair)
  check_links(links, {}, {}, [], 0, expected_active=queued, worklist_depth=32)


def test_node_labels_preserved_in_worklist():
  pairs = [
    (port(Tag.COMB, 12, 7), port(Tag.COMB, 14, 7)),
    (port(Tag.COMB, 16, 7), port(Tag.COMB, 18, 8)),
    (port(Tag.EXT_FN, 20, 0x1234), port(Tag.EXT_FN, 22, 0x1234)),
    (port(Tag.EXT_FN, 24, 0x1234), port(Tag.EXT_FN, 26, 0xABCD)),
  ]
  check_links(pairs, {}, {}, [], 0, expected_active=pairs)


def test_wire_transports_new_tags():
  value = port(Tag.EXT_VAL, (1 << 40) | 123, 11)
  graft = port(Tag.GRAFT, 41)
  comb = port(Tag.COMB, 43, 9)
  function = port(Tag.EXT_FN, 45, 13)
  check_links([(wire(1), value), (graft, wire(2)),
               (wire(3), comb), (function, wire(4))], {},
              {1: value, 2: graft, 3: comb, 4: function}, [], 4)


def test_enqueue_after_occupied_wire():
  comb = port(Tag.COMB, 20, 7)
  function = port(Tag.EXT_FN, 30, 9)
  check_links([(wire(1), function)], {1: comb}, {1: ADDR_EMPTY.value}, [1], 1,
              expected_active=[(comb, function)])


def test_enqueue_after_follow_and_swap():
  comb = port(Tag.COMB, 20, 7)
  function = port(Tag.EXT_FN, 30, 9)
  check_links([(wire(1), wire(2))], {1: function, 2: comb},
              {1: 2, 2: ADDR_EMPTY.value}, [2, 1], 2,
              expected_active=[(function, comb)])


def overflow_pairs():
  return [
    (port(Tag.COMB, 10, 1), port(Tag.COMB, 12, 1)),
    (port(Tag.EXT_FN, 14, 2), port(Tag.EXT_VAL, 123, 3)),
  ]


def test_full_worklist_fault():
  pairs = overflow_pairs()
  check_links(pairs, {}, {}, [], 0, expected_active=pairs[:1],
              worklist_depth=1, expect_overflow=True)


def test_worklist_capacity_boundary():
  pairs = overflow_pairs() + [(port(Tag.GRAFT, 7), port(Tag.COMB, 16, 4))]
  check_links(pairs, {}, {}, [], 0, expected_active=pairs[:2],
              worklist_depth=2, expect_overflow=True)


def test_full_worklist_allows_immediate_links():
  queued = overflow_pairs()[0]
  value = port(Tag.EXT_VAL, 123, 8)
  check_links([queued, (ERASE, ERASE), (value, ERASE), (wire(1), value)], {},
              {1: value}, [], 1, expected_active=[queued], worklist_depth=1)


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
    test_nonwire_dispatch_matrix,
    test_node_labels_preserved_in_worklist,
    test_wire_transports_new_tags,
    test_enqueue_after_occupied_wire,
    test_enqueue_after_follow_and_swap,
    test_full_worklist_fault,
    test_worklist_capacity_boundary,
    test_full_worklist_allows_immediate_links,
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
  print(f"Passed all {len(tests)} link tests (functional cases use 2 response delays)")
