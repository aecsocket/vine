"""Resetless, configuration-initialized memory proofs for ``verify.core``.

RAM-backed heap cases retain the actual 1024 x 64 RAM and unrestricted addresses.
A watched arbitrary address selects an independent shadow word; observations
of real RAM are *asserted*, never assumed. Heap proofs use short induction bases
(the RAM initialization is expensive for Z3); a bounded timeline covers reads
and swaps using an arbitrary pair of distinct addresses. An additional routing
proof uses an abstract two-edge resource returning arbitrary response tokens;
it covers owner changes without the full RAM's expensive reachability queries.
The native heap has two sampling edges of read latency, read-first collisions,
immediate writes, and no done.

Stack cases allow arbitrary requests (including illegal boundary operations)
and arbitrary data. Independent register-array LIFOs and live-row assertions
make the data properties inductive, including non-power-of-two capacities.
Arbiter clients retain commands until accepted, but otherwise choose arbitrary
commands/data. These are interface constraints implemented by client registers,
not assumptions about DUT internals. No runtime reset behavior is claimed.
"""

from functools import partial

from amaranth import (
  Array,
  Assert,
  Assume,
  Const,
  Cover,
  Module,
  Shape,
  Signal,
  Value,
)
from amaranth.asserts import AnyConst, AnySeq
from amaranth.hdl import Fragment
from amaranth.lib import wiring

from ivga0 import Addr, Port2, Word
from ivga0.mem.heap import Heap, HeapArbiter
from ivga0.mem.stack import Stack, StackArbiter

from .core import FormalCase


def _observe(memory, address, width, name):
  observed = Signal(width, name=name)
  memory.read_port(domain="comb", addr=address, data=observed,
                   en=Const(1), transparent_for=())
  return observed


def _heap_model(m, dut, fragment, read, read_addr, write, write_addr, write_data):
  """A universally watched word, plus an independent two-edge response model."""
  memory = fragment.find_subfragment("memory")
  watched = AnyConst(Addr)
  shadow = Signal(Word)
  with m.If(write & (write_addr == watched)):
    m.d.sync += shadow.eq(write_data)
  observed = _observe(memory, watched, Word, "heap_observed")
  m.d.comb += Assert(observed == shadow)

  pending = Signal()
  pending_known = Signal()
  pending_data = Signal(Word)
  returned = Signal()
  known = Signal(init=1)
  expected = Signal(Word)
  m.d.sync += [pending.eq(read), returned.eq(pending)]
  with m.If(read):
    m.d.sync += [pending_known.eq(read_addr == watched), pending_data.eq(shadow)]
  with m.If(pending):
    m.d.sync += [known.eq(pending_known), expected.eq(pending_data)]

  # Observe the existing BRAM output, not another synchronous read port. This
  # asserted pipeline lemma ties the shadow to the extra output register for
  # induction; it neither drives the RAM nor assumes a correct DUT response.
  bram_output = memory._read_ports[0]._data
  with m.If(pending & pending_known):
    m.d.comb += Assert(bram_output == pending_data)
  with m.If(known):
    m.d.comb += Assert(dut.read_data == expected)
  return watched, shadow, pending, pending_known, returned, known, expected


def _heap_symbolic():
  m = Module()
  dut = Heap()
  fragment = Fragment.get(dut, None)
  m.submodules.heap = fragment
  read, write = AnySeq(1), AnySeq(1)
  ra, wa, data = AnySeq(Addr), AnySeq(Addr), AnySeq(Word)
  m.d.comb += [
    dut.read.eq(read), dut.read_addr.eq(ra), dut.write.eq(write),
    dut.write_addr.eq(wa), dut.write_data.eq(data),
    Assert(dut.granted == (read | write)),
  ]
  _ = _heap_model(m, dut, fragment, read, ra, write, wa, data)
  return m


def _heap_timeline():
  """Bounded, symbolic two-address script with cycle-exact latency assertions."""
  m = Module()
  dut = m.submodules.heap = Heap()
  a, b = AnyConst(Addr), AnyConst(Addr)
  x, y, z = AnyConst(Word), AnyConst(Word), AnyConst(Word)
  m.d.comb += [Assume(a != b), Assume(x != 0), Assume(y != x),
               Assume(z != y), Assume(z != x)]
  tick = Signal(range(17))
  with m.If(tick < 16):
    m.d.sync += tick.eq(tick + 1)
  # Edge numbers are the pre-edge tick. A read at tick N is visible at N+2.
  operations = {
    0: (1, b, 1, a, x),       # initial zero read at any b, independent write at a
    1: (0, a, 1, b, y),
    3: (1, a, 0, b, z),       # delayed read; disabled addresses/data still change
    4: (1, b, 1, a, z),       # independent read/write, back-to-back with tick 3
    5: (1, a, 1, a, x),       # read-first swap must return z, not x
    6: (1, a, 0, b, y),       # must now read x
    9: (0, b, 1, b, z),       # write-only must not change read_data
    11: (1, b, 0, a, x),
  }
  m.d.comb += [dut.read.eq(0), dut.write.eq(0), dut.read_addr.eq(AnySeq(Addr)),
               dut.write_addr.eq(AnySeq(Addr)), dut.write_data.eq(AnySeq(Word))]
  for cycle, (read, ra, write, wa, data) in operations.items():
    with m.If(tick == cycle):
      m.d.comb += [dut.read.eq(read), dut.read_addr.eq(ra), dut.write.eq(write),
                   dut.write_addr.eq(wa), dut.write_data.eq(data)]
  expected = Const(0, Word)
  results = {5: x, 6: y, 7: z, 8: x, 13: z}
  for cycle in range(17):
    expected = results.get(cycle, expected)
    with m.If(tick == cycle):
      m.d.comb += Assert(dut.read_data == expected)
  m.d.comb += [
    Assert(dut.granted == (dut.read | dut.write)),
    Cover((tick == 2) & (dut.read_data == 0)),
    Cover((tick == 5) & (dut.read_data == x) & dut.read & dut.write),
    Cover((tick == 7) & (dut.read_data == z)),
    Cover((tick == 10) & (dut.read_data == x)),
    Cover((tick == 13) & (dut.read_data == z)),
    Cover(tick == 16),
  ]
  return m


def _stack_model(m, dut, fragment, depth, shape, push, pop, data):
  width = Shape.cast(shape).width
  count = Signal(range(depth + 1))
  words = Array(Signal(width, name=f"shadow_{i}") for i in range(depth))
  done = Signal()
  pop_valid = Signal()
  expected = Signal(width)
  m.d.comb += [
    Assert(count <= depth), Assert(dut.top == count),
    Assert(dut.empty == (count == 0)), Assert(dut.full == (count == depth)),
    Assert(dut.done == done), Assert(dut.granted == (push | pop)),
  ]
  m.d.sync += [done.eq(push | pop), pop_valid.eq(0)]
  with m.If(push & (count < depth)):
    m.d.sync += [words[count].eq(data), count.eq(count + 1)]
  with m.Elif(~push & pop & (count > 0)):
    m.d.sync += [expected.eq(words[count - 1]), pop_valid.eq(1), count.eq(count - 1)]
  with m.If(pop_valid):
    m.d.comb += Assert(Value.cast(dut.pop_data) == expected)

  memory = fragment.find_subfragment("memory")
  for index in range(depth):
    observed = _observe(memory, Const(index, (depth - 1).bit_length()), width,
                        f"stack_observed_{index}")
    with m.If(index < count):
      m.d.comb += Assert(observed == words[index])
  return count, pop_valid, expected


def _stack_symbolic(depth, shape):
  m = Module()
  dut = Stack(shape, depth)
  fragment = Fragment.get(dut, None)
  m.submodules.stack = fragment
  push, pop, data = AnySeq(1), AnySeq(1), AnySeq(Shape.cast(shape).width)
  m.d.comb += [dut.req_push.eq(push), dut.req_pop.eq(pop),
               Value.cast(dut.push_data).eq(data)]
  count, valid, expected = _stack_model(m, dut, fragment, depth, shape, push, pop, data)

  was_full = Signal()
  was_empty = Signal()
  was_valid = Signal()
  saved_data = Signal(Shape.cast(shape).width)
  check_hold = Signal()
  overflow = Signal()
  underflow = Signal()
  seen_full = Signal()
  with m.If(count == depth):
    m.d.sync += seen_full.eq(1)
  m.d.sync += [
    was_full.eq(count == depth), was_empty.eq(count == 0), was_valid.eq(valid),
    overflow.eq((count == depth) & push),
    underflow.eq((count == 0) & ~push & pop),
    check_hold.eq(~(~push & pop & (count > 0))),
    saved_data.eq(Value.cast(dut.pop_data)),
  ]
  with m.If(check_hold):
    m.d.comb += Assert(Value.cast(dut.pop_data) == saved_data)
  m.d.comb += [
    Cover((count == 0) & ~push & pop),
    Cover((count == depth) & push & ~pop),
    Cover((count == 0) & push & pop),
    Cover((count == depth) & push & pop),
    Cover(overflow & (count == depth) & ~push & pop),
    Cover(underflow & (count == 0) & push),
    Cover(was_full & valid & (count == depth - 1)),
    Cover(was_empty & (count == 1) & ~push & pop),
    Cover(valid & (count == 0)),
    Cover(seen_full & valid & (count == 0)),
    Cover(valid & push),
    Cover(valid & ~push & ~pop),
  ]
  if depth > 1:
    m.d.comb += [Cover((count > 0) & (count < depth) & push & pop),
                 Cover(valid & was_valid & (expected != saved_data))]
  if shape is Port2:
    # Raw 128-bit symbolic traffic exercises both fields, without restricting
    # tags to valid software-level Ports. Stack is an uninterpreted bit store.
    m.d.comb += Cover(valid & (expected[:64] != expected[64:]))
  return m


def _registered_acceptance(m, clients, fragment, wants):
  # The selected grant can otherwise remain stuck in an unreachable multi-hot
  # state during induction when no client requests. Assert its reachable-state
  # invariants as well as the externally visible mutual exclusion property.
  selection = fragment.find_subfragment("arbiter").origins[0].grant
  m.d.comb += Assert((selection & (selection - 1)) == 0)
  for i, client in enumerate(clients):
    m.d.comb += Assert(~selection[i] | wants(client))
  accepts = [client.granted for client in clients]
  accepted = Const(0)
  for grant in accepts:
    m.d.comb += Assert(~(accepted & grant))
    accepted = accepted | grant
  previous = Signal()
  m.d.sync += previous.eq(accepted)
  # A registered selection edge separates every pair of acceptances.
  m.d.comb += Assert(~(previous & accepted))
  return accepts, accepted


def _heap_arbiter():
  m = Module()
  arbiter = HeapArbiter(2)
  arbiter_fragment = Fragment.get(arbiter, None)
  m.submodules.arbiter = arbiter_fragment
  dut = Heap()
  fragment = Fragment.get(dut, None)
  m.submodules.heap = fragment
  wiring.connect(m, arbiter.real, dut.control)
  for client in arbiter.clients:
    read, write = Signal(), Signal()
    ra, wa, data = Signal(Addr), Signal(Addr), Signal(Word)
    with m.If(~(read | write) | client.granted):
      m.d.sync += [read.eq(AnySeq(1)), write.eq(AnySeq(1)),
                   ra.eq(AnySeq(Addr)), wa.eq(AnySeq(Addr)), data.eq(AnySeq(Word))]
    m.d.comb += [client.read.eq(read), client.write.eq(write),
                 client.read_addr.eq(ra), client.write_addr.eq(wa), client.write_data.eq(data)]
  accepts, accepted = _registered_acceptance(
    m, arbiter.clients, arbiter_fragment, lambda client: client.read | client.write)
  read, write = Signal(), Signal()
  ra, wa, data = Signal(Addr), Signal(Addr), Signal(Word)
  owner = Signal()
  response_owner = Signal()
  for i, (client, grant) in enumerate(zip(arbiter.clients, accepts)):
    m.d.comb += Assert(~grant | (client.read | client.write))
    with m.If(grant):
      m.d.comb += [read.eq(client.read), write.eq(client.write),
                   ra.eq(client.read_addr), wa.eq(client.write_addr), data.eq(client.write_data)]
      m.d.sync += owner.eq(i)
      m.d.comb += [Assert(dut.read_addr == client.read_addr),
                   Assert(dut.write_addr == client.write_addr), Assert(dut.write_data == data)]
  m.d.sync += response_owner.eq(owner)
  m.d.comb += [Assert(dut.read == read), Assert(dut.write == write),
               Assert(dut.granted == accepted)]
  _, _, _, _, returned, known, expected = _heap_model(
    m, dut, fragment, read, ra, write, wa, data)
  for i, client in enumerate(arbiter.clients):
    with m.If(returned & known & (response_owner == i)):
      m.d.comb += Assert(client.read_data == expected)
  return m


def _heap_arbiter_routing():
  """Protocol-only resource: arbitrary read tokens return exactly two edges later."""
  m = Module()
  arbiter = HeapArbiter(2)
  fragment = Fragment.get(arbiter, None)
  m.submodules.arbiter = fragment
  real = arbiter.real
  for client in arbiter.clients:
    read, write = Signal(), Signal()
    ra, wa, data = Signal(Addr), Signal(Addr), Signal(Word)
    with m.If(~(read | write) | client.granted):
      m.d.sync += [read.eq(AnySeq(1)), write.eq(AnySeq(1)),
                   ra.eq(AnySeq(Addr)), wa.eq(AnySeq(Addr)), data.eq(AnySeq(Word))]
    m.d.comb += [client.read.eq(read), client.write.eq(write),
                 client.read_addr.eq(ra), client.write_addr.eq(wa), client.write_data.eq(data)]
  accepts, accepted = _registered_acceptance(
    m, arbiter.clients, fragment, lambda client: client.read | client.write)

  token = AnySeq(Word)
  pending = Signal()
  bram_token = Signal(Word)
  response = Signal(Word)
  m.d.comb += [real.granted.eq(real.read | real.write), real.read_data.eq(response),
               Assert(real.granted == accepted)]
  m.d.sync += pending.eq(real.read)
  with m.If(real.read):
    m.d.sync += bram_token.eq(token)
  with m.If(pending):
    m.d.sync += response.eq(bram_token)

  expected_read, expected_write = Signal(), Signal()
  prior_reader = Signal()
  seen_response = Signal()
  for i, (client, grant) in enumerate(zip(arbiter.clients, accepts)):
    with m.If(grant):
      m.d.comb += [expected_read.eq(client.read), expected_write.eq(client.write),
                   Assert(real.read_addr == client.read_addr),
                   Assert(real.write_addr == client.write_addr),
                   Assert(real.write_data == client.write_data)]
    first, second = Signal(name=f"valid1_{i}"), Signal(name=f"valid2_{i}")
    first_data, second_data = Signal(Word), Signal(Word)
    m.d.sync += [first.eq(grant & client.read), second.eq(first),
                 first_data.eq(token), second_data.eq(first_data)]
    with m.If(second):
      m.d.comb += Assert(client.read_data == second_data)
      m.d.sync += [prior_reader.eq(i), seen_response.eq(1)]
    m.d.comb += [
      Cover(second & (second_data != 0)),
      Cover(second & accepts[1 - i]),
      Cover(second & seen_response & (prior_reader != i) & (second_data != 0)),
      Cover(grant & client.read & client.write),
    ]
  m.d.comb += [Assert(real.read == expected_read), Assert(real.write == expected_write)]
  return m


def _stack_arbiter():
  m = Module()
  depth, shape = 3, Word
  arbiter = StackArbiter(2, shape)
  arbiter_fragment = Fragment.get(arbiter, None)
  m.submodules.arbiter = arbiter_fragment
  dut = Stack(shape, depth)
  fragment = Fragment.get(dut, None)
  m.submodules.stack = fragment
  wiring.connect(m, arbiter.real, dut.control)
  for client in arbiter.clients:
    push, pop, data = Signal(), Signal(), Signal(shape)
    with m.If(~(push | pop) | client.granted):
      m.d.sync += [push.eq(AnySeq(1)), pop.eq(AnySeq(1)), data.eq(AnySeq(shape))]
    m.d.comb += [client.req_push.eq(push), client.req_pop.eq(pop), client.push_data.eq(data)]
  accepts, accepted = _registered_acceptance(
    m, arbiter.clients, arbiter_fragment, lambda client: client.req_push | client.req_pop)
  push, pop, data = Signal(), Signal(), Signal(shape)
  owner = Signal()
  for i, (client, grant) in enumerate(zip(arbiter.clients, accepts)):
    m.d.comb += Assert(~grant | (client.req_push | client.req_pop))
    with m.If(grant):
      m.d.comb += [push.eq(client.req_push), pop.eq(client.req_pop), data.eq(client.push_data)]
      m.d.sync += owner.eq(i)
  m.d.comb += [Assert(dut.req_push == push), Assert(dut.req_pop == pop),
               Assert(~push | (dut.push_data == data)), Assert(dut.granted == accepted)]
  count, valid, expected = _stack_model(m, dut, fragment, depth, shape, push, pop, data)
  previous_accepts = Signal(2)
  for i, (client, grant) in enumerate(zip(arbiter.clients, accepts)):
    m.d.sync += previous_accepts[i].eq(grant)
    m.d.comb += [Assert(client.empty == (count == 0)), Assert(client.full == (count == depth)),
                 Assert(client.done == previous_accepts[i])]
    with m.If(valid & (owner == i)):
      m.d.comb += Assert(client.pop_data == expected)
    m.d.comb += [Cover(valid & (owner == i)),
                 Cover(grant & client.req_push & client.req_pop & (count == depth))]
  m.d.comb += [Cover(valid & (count == 0)),
               Cover((count == depth) & pop & ~push)]
  return m


def cases() -> list[FormalCase]:
  return [
    FormalCase("memory_heap_symbolic_1024x64", _heap_symbolic, depth=3, cover=False),
    FormalCase("memory_heap_timeline_1024x64_bmc", _heap_timeline, depth=18, mode="bmc"),
    *[FormalCase(f"memory_stack_lifo_d{depth}_u64", partial(_stack_symbolic, depth, Word),
                 depth=2 * depth + 6)
      for depth in (1, 2, 3, 5)],
    FormalCase("memory_stack_lifo_d2_port2", partial(_stack_symbolic, 2, Port2), depth=10),
    FormalCase("memory_heap_arbiter2_1024x64", _heap_arbiter, depth=3, cover=False),
    FormalCase("memory_heap_arbiter2_routing_tokens", _heap_arbiter_routing, depth=12),
    FormalCase("memory_stack_arbiter2_d3_u64", _stack_arbiter, depth=18),
  ]
