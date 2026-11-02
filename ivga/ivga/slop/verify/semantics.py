"""Small semantic optimization baselines, not a whole-runtime proof.

Run: python -B -m ivga.slop.verify.semantics
Add --regressions to also run the two deliberately failing commute regressions.

All transaction cases are BMC from configuration-time initialization, with an
explicit completion deadline and several idle cycles inside the checked bound.
Symbolic operands are AnyConst, not sampled examples. Downstream linker clients
are modeled at their public protocol boundary: grants and completions can stall
independently for 0..3 cycles (N32), or 0..1 cycles (Interact dispatch), including
skewed, one-cycle completion pulses.
No assumption constrains a DUT output or supplies the expected result as a
service response. Stack pushes complete on grant, unlike linker operations.

Interact cases elaborate its real program/dispatch, but replace its eight leaf
linkers with protocol services. Linker cases instantiate the real component.
These are complementary cutpoints, not an integrated heap/allocator proof.
Excluded: wire following/swaps, graft execution, branch table reads, call and
continuation dispatch, allocator ownership, reset, repeated transactions, and
whole Runtime/Fib. The live registry has no known non-copyable type; a synthetic
mixed-registry lookup checks that policy separately. Commute is NOT certified:
regression_cases() checks its B-node identity and missing completion independently.
Only the stateless packing/lookup properties are labeled prove; sequential
properties deliberately advertise their finite bounds.
"""

import argparse
from functools import partial
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from amaranth import Assert, Cat, Const, Cover, Module, Mux, Signal, Value
from amaranth.asserts import AnyConst, AnySeq
from amflow import ValueLike, expand

import ivga.extrinsic as registry
from ivga import ADDR_WIDTH, Port, Tag
from ivga.extrinsic import n32
from ivga.extrinsic.common import ExtFnContext, ExtTy
from ivga.mem.heap import HeapPort
from ivga.mem.pair_alloc import PairAllocPort
from ivga.runtime import interact
from ivga.runtime.fault import FaultPort
from ivga.runtime.grafter import GrafterPort
from ivga.runtime.linker import Linker, LinkerPort
from ivga.slop.verify.core import FormalCase, run_case


def _bits(value: ValueLike, width: int) -> Value:
  return Value.cast(Cat(Value.cast(value), Const(0, width))[:width])


def _port(tag: ValueLike, payload: ValueLike = 0, label: ValueLike = 0) -> Port:
  """Independent packed-port oracle (does not call Port.new/aux)."""
  return Port(Cat(_bits(tag, 3), _bits(payload, 45), _bits(label, 16)))


def _age(m, limit):
  age = Signal(range(limit + 1), name="age")
  with m.If(age < limit):
    m.d.sync += age.eq(age + 1)
  return age


def _copy_policy(label, types):
  result = Const(0)
  for i, entry in enumerate(types):
    if entry.is_copy:
      result = result | (label == i)
  return result


def _link_service(m, port, expected=None, *, name="link", fixed=False, stall_bits=2):
  """One legal transaction; readiness after 0..(2**stall_bits-1) stalls.

  fixed=True uses exactly two grant stalls and two completion stalls. A done
  pulse can first occur in the cycle AFTER grant. accepted/completed latch for
  the rest of the experiment, making replay a checked failure, not a new request.
  """
  accepted = Signal(name=f"{name}_accepted")
  completed = Signal(name=f"{name}_completed")
  grant_age = Signal(2, name=f"{name}_grant_age")
  done_age = Signal(2, name=f"{name}_done_age")
  saved_a, saved_b = Signal(64), Signal(64)
  held_a, held_b = Signal(64), Signal(64)
  stalled = Signal()
  saw_stall = Signal(name=f"{name}_saw_stall")
  # With one transaction, independent symbolic delay constants enumerate ALL
  # bounded variable-ready histories: readiness before the accepting edge is
  # unobservable. They avoid introducing irrelevant fresh bits on idle cycles.
  grant_ready = grant_age == (Const(2, 2) if fixed else AnyConst(stall_bits))
  done_ready = done_age == (Const(2, 2) if fixed else AnyConst(stall_bits))
  grant = port.req_link & ~accepted & grant_ready
  finish = accepted & ~completed & done_ready
  m.d.comb += [port.granted.eq(grant), port.done.eq(finish)]
  m.d.sync += [
    stalled.eq(port.req_link & ~grant),
    held_a.eq(port.req_a.as_value()), held_b.eq(port.req_b.as_value()),
  ]
  with m.If(port.req_link & ~accepted & ~grant):
    m.d.sync += [grant_age.eq(grant_age + 1), saw_stall.eq(1)]
  with m.If(grant):
    m.d.sync += [accepted.eq(1), saved_a.eq(port.req_a.as_value()),
                 saved_b.eq(port.req_b.as_value())]
  with m.If(accepted & ~completed & ~finish):
    m.d.sync += done_age.eq(done_age + 1)
  with m.If(finish):
    m.d.sync += completed.eq(1)
  with m.If(stalled):
    m.d.comb += [Assert(port.req_link), Assert(port.req_a.as_value() == held_a),
                 Assert(port.req_b.as_value() == held_b)]
  with m.If(accepted):
    m.d.comb += Assert(~port.req_link)
  with m.If(accepted & ~completed):
    m.d.comb += [Assert(port.req_a.as_value() == saved_a),
                 Assert(port.req_b.as_value() == saved_b)]
  if expected is not None:
    with m.If(port.req_link):
      m.d.comb += [Assert(port.req_a.as_value() == expected[0].as_value()),
                   Assert(port.req_b.as_value() == expected[1].as_value())]
  return SimpleNamespace(accepted=accepted, completed=completed, stall=saw_stall,
                         grant_age=grant_age, done_age=done_age)


def _completion(m, done, services, *, age, deadline, stat=None):
  seen = Signal(name="completion_seen")
  all_completed = Const(1)
  for service in services:
    all_completed = all_completed & service.accepted & service.completed
  with m.If(done):
    m.d.comb += [Assert(~seen), Assert(all_completed)]
    m.d.sync += seen.eq(1)
  with m.If(age >= deadline):
    m.d.comb += Assert(seen)
  if stat is not None:
    m.d.comb += Assert(stat == (seen | done))
  m.d.comb += Cover(seen & (age == deadline))
  return seen


def _packing():
  m = Module()
  tag, payload, label = AnyConst(3), AnyConst(45), AnyConst(16)
  packed = Port.new(tag, label, payload)
  # Include tag bit pattern 7: packing is a bit-level contract, not tag validity.
  m.d.comb += [
    Assert(packed.as_value() == (tag | (payload << 3) | (label << 48))),
    Assert(packed.tag.as_value() == tag), Assert(packed.payload == payload),
    Assert(packed.label == label),
    Assert(Port.with_addr(payload).as_value() == (payload << 3)),
    Assert(Port.none().as_value() == 0),
    Assert(Port.eraser().as_value() == Tag.ERASE.value),
    Assert(Port.freed().as_value() == 0xFFFF_FFFF_FFFF_FFF8),
    Cover((tag == 7) & (payload == (1 << 45) - 1) & (label == 0xFFFF)),
  ]
  return m


def _auxiliary():
  m = Module()
  raw = AnyConst(64)
  source = Port(raw)
  a, b = Port.aux(source)
  m.d.comb += [
    Assert(a.as_value() == (Const(Tag.WIRE.value, 64) | (source.payload << 3))),
    Assert(b.as_value() == (Const(Tag.WIRE.value, 64) | ((source.payload ^ 1) << 3))),
    Assert((a.payload ^ b.payload) == 1),
    Assert((a.payload & ~1) == (b.payload & ~1)),
    Cover(source.payload == (1 << 45) - 1), Cover(source.payload == 0),
  ]
  return m


def _lookup(*, mixed=False):
  m = Module()
  label = AnyConst(16)
  types = ([ExtTy(f"formal:{i}", is_copy=copy) for i, copy in
            enumerate((False, True, False, True, True, False))]
           if mixed else registry.EXT_TYS)
  mask = sum(int(entry.is_copy) << i for i, entry in enumerate(types))
  # Patching is elaboration-local; no production registry is changed on disk.
  with patch.object(registry, "EXT_TYS", types), patch.object(registry, "COPY_MASK", mask):
    actual = registry.ext_val_label_is_copy(label)
  expected = _copy_policy(label, types)
  m.d.comb += [Assert(actual == expected), Cover(expected),
               Cover((label == 0xFFFF) & ~actual),
               Cover((label == len(types)) & ~actual)]
  if mixed:
    m.d.comb += Cover((label == 2) & ~actual)
  return m


def _n32(operation, *, fixed=False):
  m = Module()
  age = _age(m, 21)
  a, b = AnyConst(32), AnyConst(32)
  # Upper payload bits are independently symbolic: handlers consume low 32 bits.
  pa = _port(Tag.EXT_VAL, Value.cast(Cat(a, AnyConst(13))), 0xB731)
  pb = _port(Tag.EXT_VAL, Value.cast(Cat(b, AnyConst(13))), 0xB731)
  out = _port(Tag.WIRE, AnyConst(ADDR_WIDTH))
  heap, link = HeapPort().create(), LinkerPort().create()
  done = Signal()
  m.d.sync += done.eq(0)
  context = []
  entry = next(entry for entry in n32.EXT_FNS if entry.name == f"vi:n32:{operation}")

  def program(f):
    cx = ExtFnContext(f, heap, link, pa, pb, out)
    cx.ty_id[n32.N32] = 0xB731  # Non-default ID catches hardcoded result labels.
    context.append(cx)
    # Exercise clearing a pre-existing error, without driving the handler's error
    # register from a second controller after the operation begins.
    f.d.sync += cx.error.eq(1)
    yield
    yield from entry.fn(cx)
    f.d.sync += done.eq(1)
    yield
    f.d.until += Const(0)
    yield

  expand(m, program)
  oracle = {"add": lambda: a + b, "sub": lambda: a - b,
            "eq": lambda: a == b, "ne": lambda: a != b}[operation]()
  expected = _port(Tag.EXT_VAL, _bits(oracle, 32), 0xB731)
  service = _link_service(m, link, (out, expected), fixed=fixed)
  seen = _completion(m, done, [service], age=age, deadline=16)
  m.d.comb += [Assert(~heap.read), Assert(~heap.write)]
  with m.If(done | seen):
    m.d.comb += Assert(~context[0].error)
  max_stall = 2 if fixed else 3
  m.d.comb += [Cover(seen & service.stall),
               Cover(seen & (service.grant_age == max_stall) & (service.done_age == max_stall))]
  if operation == "add":
    m.d.comb += [Cover(seen & ((Cat(a, Const(0, 1)) + b) > 0xFFFF_FFFF)),
                 Cover(seen & (a == 0xFFFF_FFFF) & (b == 1))]
  elif operation == "sub":
    m.d.comb += [Cover(seen & (a < b)), Cover(seen & (a == 0) & (b == 1))]
  else:
    m.d.comb += [Cover(seen & (a == b)), Cover(seen & (a != b))]
  return m


def _quiet_linker(m, dut):
  m.d.comb += [
    dut.heap.granted.eq(0), dut.heap.read_data.eq(AnySeq(64)),
    dut.alloc.granted.eq(0), dut.alloc.done.eq(0), dut.alloc.oom.eq(0),
    dut.fault.halt.eq(0), dut.stat_reset.eq(0),
    Assert(~dut.heap.read), Assert(~dut.heap.write),
    Assert(dut.alloc.req_alloc == 0), Assert(dut.alloc.req_free == 0),
    Assert(~dut.active.req_pop),
  ]


def _linker(kind):
  m = Module()
  dut = m.submodules.dut = Linker()
  age = _age(m, 23)
  if kind in ("graft_erase", "ext_erase"):
    tag = Tag.GRAFT if kind == "graft_erase" else Tag.EXT_VAL
    a = _port(Mux(AnyConst(1), tag, Tag.ERASE), AnyConst(45), AnyConst(16))
    b = _port(Mux(AnyConst(1), tag, Tag.ERASE), AnyConst(45), AnyConst(16))
  else:
    # All combinations reach the active enqueue path, in original operand order.
    a = _port(Mux(AnyConst(1), Tag.COMB, Tag.EXT_FN), AnyConst(ADDR_WIDTH), AnyConst(16))
    b = _port(Mux(AnyConst(1), Tag.COMB, Tag.EXT_VAL), AnyConst(45), AnyConst(16))
  accepted = Signal()
  with m.If(dut.req_link & dut.granted):
    m.d.sync += accepted.eq(1)
  # The caller may change everything after acceptance. Nothing is assumed about
  # the request payload while the linker is busy.
  m.d.comb += [dut.req_link.eq(~accepted),
               dut.req_a.eq(Mux(accepted, AnySeq(64), a.as_value())),
               dut.req_b.eq(Mux(accepted, AnySeq(64), b.as_value()))]
  _quiet_linker(m, dut)
  full = AnyConst(1) if kind == "active" else Const(0)
  m.d.comb += [dut.active.full.eq(full), dut.active.empty.eq(1),
               dut.active.pop_data.eq(0)]
  pushed, faulted = Signal(), Signal()
  push_age = Signal(2)
  ready = AnySeq(1) | (push_age == 3)
  grant = dut.active.req_push & ready & ~pushed
  m.d.comb += [dut.active.granted.eq(grant), dut.active.done.eq(0)]
  with m.If(dut.active.req_push & ~grant):
    m.d.sync += push_age.eq(push_age + 1)
  with m.If(dut.active.req_push):
    m.d.comb += [Assert(kind == "active"), Assert(~full), Assert(~pushed),
                 Assert(dut.active.push_data.a.as_value() == a.as_value()),
                 Assert(dut.active.push_data.b.as_value() == b.as_value())]
  with m.If(grant):
    m.d.sync += pushed.eq(1)
  with m.If(dut.fault.active_full):
    m.d.sync += faulted.eq(1)
  if kind == "active":
    m.d.comb += Assert(~dut.fault.active_full | full)
    completed = SimpleNamespace(accepted=accepted, completed=Mux(full, faulted, pushed))
    seen = _completion(m, dut.done, [completed], age=age, deadline=17)
    m.d.comb += [Assert(dut.stat_erase == 0),
                 Cover(seen & full & faulted & ~pushed),
                 Cover(seen & ~full & pushed & (push_age == 3))]
  else:
    m.d.comb += [Assert(~dut.active.req_push), Assert(~dut.fault.active_full)]
    seen = _completion(m, dut.done, [], age=age, deadline=12, stat=dut.stat_erase)
    m.d.comb += [Cover(seen & (a.tag == Tag.ERASE) & (b.tag == Tag.ERASE)),
                 Cover(seen & (a.tag != Tag.ERASE) & (b.tag != Tag.ERASE)),
                 Cover(seen & (a.tag == Tag.ERASE) & (b.tag != Tag.ERASE)),
                 Cover(seen & (a.tag != Tag.ERASE) & (b.tag == Tag.ERASE))]
  return m


def _interact_shell(m):
  """The real Interact controller, with leaf services intentionally abstracted."""
  dut = SimpleNamespace(
    req_interact=Signal(), req_a=Signal(Port), req_b=Signal(Port),
    granted=Signal(), done=Signal(), grafting=Signal(),
    stat_annihilate=Signal(32), stat_copy=Signal(32), stat_commute=Signal(32),
    stat_graft=Signal(32), stat_extrinsic=Signal(32),
    link_ports=[LinkerPort().create() for _ in range(interact.LINKERS)],
    own_heap=HeapPort().create(), own_pairs=PairAllocPort().create(),
    grafter=GrafterPort().create(), fault=FaultPort().create(),
  )
  m.d.sync += dut.done.eq(0)
  expand(m, lambda f: interact.program(f, cast(interact.Interact, cast(object, dut))))
  m.d.comb += [dut.fault.halt.eq(0), dut.own_heap.granted.eq(0),
               dut.own_heap.read_data.eq(AnySeq(64)), dut.grafter.granted.eq(0),
               dut.grafter.done.eq(0), dut.grafter.oom.eq(0), dut.grafter.error.eq(0)]
  return dut


def _interact_input(m, dut, a, b):
  accepted = Signal()
  with m.If(dut.req_interact & dut.granted):
    m.d.sync += accepted.eq(1)
  # Change addresses/data after acceptance but keep tags/labels fixed. This
  # still checks capture, while allowing synthesis to specialize the dispatch
  # cutpoint instead of retaining every unrelated graft/call controller.
  poison_a = _port(a.tag, Mux(accepted, AnySeq(45), a.payload), a.label)
  poison_b = _port(b.tag, Mux(accepted, AnySeq(45), b.payload), b.label)
  m.d.comb += [dut.req_interact.eq(~accepted),
               dut.req_a.eq(poison_a.as_value()), dut.req_b.eq(poison_b.as_value())]


def _interact(kind, *, reverse=False):
  m = Module()
  age = _age(m, 17)
  dut = _interact_shell(m)
  # Nodes have legal even bases. Aux views are checked against XOR, not addition.
  base_a = Value.cast(Cat(Const(0, 1), AnyConst(ADDR_WIDTH - 1)))
  base_b = Value.cast(Cat(Const(0, 1), AnyConst(ADDR_WIDTH - 1)))
  label = AnyConst(16)
  node = _port(Tag.COMB, base_b, label)
  allowed = Const(1)
  value = _port(Tag.ERASE)
  if kind == "annihilate":
    a, b = _port(Tag.COMB, base_a, label), node
    expected = [(_port(Tag.WIRE, base_a), _port(Tag.WIRE, base_b)),
                (_port(Tag.WIRE, base_a ^ 1), _port(Tag.WIRE, base_b ^ 1))]
    stat = dut.stat_annihilate
  else:
    if kind == "ext":
      value = _port(Tag.EXT_VAL, AnyConst(45), AnyConst(16))
      allowed = _copy_policy(value.label, registry.EXT_TYS)
      result = Port(Mux(allowed, value.as_value(), _port(Tag.ERASE).as_value()))
    elif kind == "graft":
      value = _port(Tag.GRAFT, AnyConst(ADDR_WIDTH))
      # GRAFT copying requires a nonzero COMB label.
      node = _port(Tag.COMB, base_b, label | 1)
      result = value
    else:
      value = result = _port(Tag.ERASE)
    a, b = (node, value) if reverse else (value, node)
    expected = [(_port(Tag.WIRE, base_b), result),
                (_port(Tag.WIRE, base_b ^ 1), result)]
    stat = dut.stat_copy
  _interact_input(m, dut, a, b)
  m.d.comb += [dut.own_pairs.granted.eq(0), dut.own_pairs.done.eq(0),
               dut.own_pairs.oom.eq(0), Assert(~dut.own_heap.read),
               Assert(~dut.own_heap.write), Assert(dut.own_pairs.req_alloc == 0),
               Assert(dut.own_pairs.req_free == 0), Assert(~dut.grafter.req_graft),
               Assert(~dut.fault.invalid_interaction), Assert(dut.stat_commute == 0),
               Assert(dut.stat_graft == 0), Assert(dut.stat_extrinsic == 0)]
  services = [_link_service(m, port, pair, name=f"link{i}", stall_bits=1)
              for i, (port, pair) in enumerate(zip(dut.link_ports, expected))]
  for port in dut.link_ports[2:]:
    m.d.comb += [Assert(~port.req_link), port.granted.eq(0), port.done.eq(0)]
  seen = _completion(m, dut.done, services, age=age, deadline=12, stat=stat)
  unused_stat = dut.stat_copy if kind == "annihilate" else dut.stat_annihilate
  m.d.comb += Assert(unused_stat == 0)
  faulted = Signal()
  with m.If(dut.fault.ext_copy):
    m.d.sync += faulted.eq(1)
  with m.If(dut.done | seen):
    m.d.comb += [Assert(faulted == ~allowed), Assert(~dut.fault.ext_copy)]
  with m.If(dut.fault.ext_copy):
    m.d.comb += Assert(~allowed)
  m.d.comb += [Cover(seen & services[0].stall & services[1].stall),
               Cover(services[0].completed & ~services[1].completed),
               Cover(services[1].completed & ~services[0].completed),
               Cover(seen & (base_b == (1 << ADDR_WIDTH) - 2))]
  if kind == "ext":
    m.d.comb += [Cover(seen & allowed & ~faulted),
                 Cover(seen & ~allowed & faulted),
                 Cover(seen & (value.label == 0xFFFF) & faulted)]
  return m


def _commute_regression(*, completion=False):
  """Expected FAIL, never included in cases(): specification, not current output."""
  m = Module()
  age = _age(m, 29)
  dut = _interact_shell(m)
  a = _port(Tag.COMB, 16, 0x1234)
  b = _port(Tag.EXT_FN, 32, 0)  # Registered function ID 0, not an invalid call label.
  _interact_input(m, dut, a, b)
  alloc = dut.own_pairs
  allocated = Signal()
  alloc_completed = Signal()
  m.d.comb += [alloc.granted.eq((alloc.req_alloc != 0) & ~allocated),
               alloc.done.eq(allocated & ~alloc_completed), alloc.oom.eq(0)]
  with m.If(alloc.granted):
    m.d.comb += Assert(alloc.req_alloc == 4)
    m.d.sync += allocated.eq(1)
  with m.If(alloc.done):
    m.d.sync += alloc_completed.eq(1)
  bases = (64, 66, 68, 70)
  for i, base in enumerate(bases):
    m.d.comb += alloc.alloc_bases[i].eq(base)
  m.d.comb += [Assert(~dut.own_heap.read), Assert(~dut.own_heap.write),
               Assert(alloc.req_free == 0), Assert(~dut.grafter.req_graft)]
  w = lambda address: _port(Tag.WIRE, address)
  expected = [(w(64), w(68)), (w(65), w(70)),
              (w(66), w(69)), (w(67), w(71)),
              (w(16), _port(b.tag, 68, b.label)),
              (w(17), _port(b.tag, 70, b.label)),
              (w(32), _port(a.tag, 64, a.label)),
              (w(33), _port(a.tag, 66, a.label))]
  # Completion is an independent regression: omit payload assertions there so
  # the earlier identity failure cannot mask missing done/stat_commute.
  services = [_link_service(m, port, None if completion else pair, name=f"link{i}", fixed=True)
              for i, (port, pair) in enumerate(zip(dut.link_ports, expected))]
  if completion:
    _ = _completion(m, dut.done, services, age=age, deadline=18, stat=dut.stat_commute)
  else:
    m.d.comb += Cover(services[4].accepted)
  return m


def cases() -> list[FormalCase]:
  """Passing baselines; a BMC pass must not be described as an unbounded proof."""
  result = [
    FormalCase("semantics_port_pack", _packing, depth=4),
    FormalCase("semantics_port_aux", _auxiliary, depth=4),
    FormalCase("semantics_copy_lookup", _lookup, depth=4),
    FormalCase("semantics_copy_lookup_mixed", partial(_lookup, mixed=True), depth=4),
  ]
  for operation in ("add", "sub", "eq", "ne"):
    result.append(FormalCase(f"semantics_n32_{operation}", partial(_n32, operation),
                             depth=22, mode="bmc"))
  result.append(FormalCase("semantics_n32_add_fixed", partial(_n32, "add", fixed=True),
                           depth=22, mode="bmc"))
  for kind in ("graft_erase", "ext_erase", "active"):
    result.append(FormalCase(f"semantics_linker_{kind}", partial(_linker, kind),
                             depth=24, mode="bmc"))
  result.append(FormalCase("semantics_interact_annihilate", partial(_interact, "annihilate"),
                           depth=18, mode="bmc"))
  for kind in ("erase", "graft", "ext"):
    for reverse in (False, True):
      result.append(FormalCase(f"semantics_interact_{kind}_{'ba' if reverse else 'ab'}",
                               partial(_interact, kind, reverse=reverse), depth=18, mode="bmc"))
  return result


def regression_cases() -> list[FormalCase]:
  """Known real bugs, expected counterexamples; no xfail or claimed proof."""
  return [
    FormalCase("semantics_regression_commute_identity", _commute_regression,
               depth=22, mode="bmc"),
    FormalCase("semantics_regression_commute_completion",
               partial(_commute_regression, completion=True), depth=22, mode="bmc"),
  ]


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  _ = parser.add_argument("--regressions", action="store_true", help="also run known failing specifications")
  args = parser.parse_args()
  selected = cases() + (regression_cases() if args.regressions else [])
  failed = False
  for case in selected:
    result = run_case(case)
    print(f"{'PASS' if result.passed else 'FAIL'} {case.name}: {result.detail} ({result.seconds:.2f}s)",
          flush=True)
    failed |= not result.passed
  return int(failed)


if __name__ == "__main__":
  raise SystemExit(main())
