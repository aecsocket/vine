"""Stack proofs: run `python -m ivga.mem.test_stack`.

Requests are sampled on each edge, including held and simultaneous requests.
Push has priority; overflow/underflow acknowledge without changing the stack.
A successful pop must return its data alongside done on the following cycle,
independently of that cycle's requests. Invalid pop data is unspecified.

Directed scripts use a Python LIFO oracle. Symbolic proofs check arbitrary
requests and data, with asserted live-memory invariants for induction.
"""

import subprocess
from functools import partial

from amaranth import Array, Assert, Const, Cover, Shape, Signal, signed, unsigned
from amaranth.asserts import AnySeq
from amaranth.hdl import Fragment
from ivga.mem.stack import Stack
from ivga.verify import Verify

from ivga import Word

DEFAULT_SHAPE = unsigned(8)


def setup(depth=4, shape=DEFAULT_SHAPE, cycles=8):
  dut = Stack(shape, depth)
  # Reuse this fragment so induction observes the actual DUT memory rows.
  fragment = Fragment.get(dut, platform=None)
  memory = fragment.find_subfragment("memory")
  verify = Verify(fragment, cycles=cycles)
  m = verify.m
  m.d.comb += [
    dut.req_push.eq(False),
    dut.req_pop.eq(False),
    dut.push_data.eq(0),
    Assert(dut.top <= depth),
    Assert(dut.empty == (dut.top == 0)),
    Assert(dut.full == (dut.top == depth)),
  ]
  with m.If(verify.cycle == 0):
    m.d.comb += [Assert(dut.top == 0), Assert(~dut.done)]
  return dut, verify, memory


def verify_sequence(name, operations, depth=4):
  """Each tuple is ('push', data), ('pop',), ('both', data), or ('idle',)."""
  dut, verify, _ = setup(depth=depth, cycles=len(operations) + 2)
  m, cycle = verify.m, verify.cycle
  stack = []

  for tick, operation in enumerate(operations):
    kind = operation[0]
    assert kind in ("push", "pop", "both", "idle")
    push = kind in ("push", "both")
    pop = kind in ("pop", "both")
    with m.If(cycle == tick):
      m.d.comb += [dut.req_push.eq(push), dut.req_pop.eq(pop)]
      if push:
        m.d.comb += dut.push_data.eq(operation[1])

    popped = None
    if push:
      if len(stack) < depth:
        stack.append(operation[1])
    elif pop and stack:
      popped = stack.pop()

    with m.If(cycle == tick + 1):
      m.d.comb += [
        Assert(dut.top == len(stack)),
        Assert(dut.done == (push or pop)),
      ]
      if popped is not None:
        m.d.comb += Assert(dut.pop_data == popped)

  with m.If(cycle > len(operations)):
    m.d.comb += [Assert(~dut.done), Assert(dut.top == len(stack))]
  m.d.comb += Cover(cycle == len(operations) + 1)
  verify.prove(name, depth=len(operations) + 4)


def verify_idle():
  verify_sequence("verify_stack_idle", [("idle",)] * 4)


def verify_underflow():
  verify_sequence("verify_stack_underflow", [("pop",)] * 3 + [("idle",)])


def verify_lifo():
  verify_sequence("verify_stack_lifo", [
    ("push", 0x12), ("idle",), ("push", 0xA5), ("push", 0xFF),
    ("idle",), ("pop",), ("idle",), ("pop",), ("idle",), ("pop",),
  ])


def verify_consecutive_pops():
  verify_sequence("verify_stack_consecutive_pops", [
    ("push", 0x12), ("push", 0x34), ("push", 0x56),
    ("pop",), ("pop",), ("pop",), ("pop",),
  ])


def verify_overflow_recovery():
  verify_sequence("verify_stack_overflow_recovery", [
    ("push", 0x12), ("push", 0x34), ("push", 0x56),
    ("push", 0x78), ("push", 0x9A), ("pop",), ("push", 0xBC),
    ("pop",), ("pop",), ("pop",), ("pop",), ("pop",),
  ], depth=3)


def verify_interleaved_reuse():
  verify_sequence("verify_stack_interleaved_reuse", [
    ("push", 0x12), ("push", 0x34), ("pop",), ("push", 0x56),
    ("pop",), ("pop",), ("push", 0), ("pop",), ("idle",),
  ])


def verify_push_priority():
  # Simultaneous requests at empty, partially occupied, and full boundaries.
  verify_sequence("verify_stack_push_priority", [
    ("both", 0x12), ("both", 0x34), ("both", 0x56),
    ("both", 0x78), ("idle",),
  ], depth=3)


def verify_arbitrary_requests(depth, shape=DEFAULT_SHAPE, check_data=True):
  shape = Shape.cast(shape)
  suffix = f"{depth}_{'s' if shape.signed else 'u'}{shape.width}"
  name = f"verify_stack_{'lifo' if check_data else 'control'}_{suffix}"
  cycles = 2 * depth + 5
  dut, verify, memory = setup(depth=depth, shape=shape, cycles=cycles)
  m = verify.m
  count = Signal(range(depth + 1))
  done = Signal()
  pop_valid = Signal()
  pop_data = Signal(shape)
  words = Array(Signal(shape, name=f"expected_{i}") for i in range(depth))
  push = AnySeq(1)
  pop = AnySeq(1)
  data = AnySeq(shape.width)

  m.d.comb += [
    dut.req_push.eq(push),
    dut.req_pop.eq(pop),
    dut.push_data.eq(data),
    Assert(dut.top == count),
    Assert(dut.empty == (count == 0)),
    Assert(dut.full == (count == depth)),
    Assert(dut.done == done),
  ]
  m.d.sync += [done.eq(push | pop), pop_valid.eq(False)]
  with m.If(push & (count < depth)):
    m.d.sync += [words[count].eq(data), count.eq(count + 1)]
  with m.Elif(~push & pop & (count > 0)):
    m.d.sync += [
      pop_data.eq(words[count - 1]),
      pop_valid.eq(True),
      count.eq(count - 1),
    ]

  # Read-only observation ports expose live RAM rows to asserted invariants.
  # They strengthen induction without assuming correctness or driving the DUT.
  for index in range(depth):
    observed = Signal(shape, name=f"observed_{index}")
    memory.read_port(domain="comb", addr=Const(index, (depth - 1).bit_length()),
                     data=observed, en=Const(1), transparent_for=())
    with m.If(index < count):
      m.d.comb += Assert(observed == words[index])
  if check_data:
    with m.If(pop_valid):
      m.d.comb += Assert(dut.pop_data == pop_data)

  m.d.comb += [
    Cover((count == depth) & push & ~pop),
    Cover((count == 0) & ~push & pop),
    Cover((count == 0) & push & pop),
    Cover((count == depth) & push & pop),

    Cover(pop_valid & ~push & ~pop),
    Cover(pop_valid & push),
    Cover(pop_valid & (count == 0)),
  ]
  if depth > 1:
    m.d.comb += [
      Cover((count > 0) & (count < depth) & push & pop),
      Cover(pop_valid & ~push & pop & (count > 0)),
    ]
  verify.prove(name, depth=cycles + 1)


if __name__ == "__main__":
  tests = [
    verify_idle,
    verify_underflow,
    verify_lifo,
    verify_consecutive_pops,
    verify_overflow_recovery,
    verify_interleaved_reuse,
    verify_push_priority,
  ]
  for depth in (1, 3, 4):
    tests += [
      partial(verify_arbitrary_requests, depth, check_data=False),
      partial(verify_arbitrary_requests, depth),
    ]
  tests += [
    partial(verify_arbitrary_requests, 3, signed(8)),
    partial(verify_arbitrary_requests, 2, Word),
  ]
  failures = 0
  for test in tests:
    try:
      test()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
      failures += 1
      label = f"{test.func.__name__}{test.args} {test.keywords}" if isinstance(test, partial) else test.__name__
      print(f"FAIL {label}: {error}", flush=True)
  print(f"Stack proofs: {len(tests) - failures} passed, {failures} failed", flush=True)
  raise SystemExit(bool(failures))
