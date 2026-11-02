"""Bounded symbolic control-flow and HeapPort-helper contracts.

Ready is allowed at most one consecutive stall. Inputs are otherwise arbitrary;
held-data tests deliberately change live sources after capture. These exercise
the real amflow lowering, not an alternate FSM. Deadlines assert progress; cover
goals additionally require meaningful paths to be reachable.
"""

from functools import partial

from amaranth import (
  Array,
  Assert,
  Assume,
  Cat,
  Const,
  Cover,
  Module,
  Mux,
  Signal,
  Value,
  unsigned,
)
from amaranth.asserts import AnyConst, AnySeq
from amaranth.lib.data import ArrayLayout
from amflow import expand

from ivga import Addr, Word
from ivga.mem.heap import HeapPort

from .core import FormalCase


def _age(m, limit=24):
  age = Signal(range(limit + 1))
  with m.If(age < limit):
    m.d.sync += age.eq(age + 1)
  return age


def _ready(m):
  ready = AnySeq(1)
  stalled = Signal()
  m.d.comb += Assume(~stalled | ready)
  m.d.sync += stalled.eq(~ready)
  return ready


def _finish(f, done):
  f.d.sync += done.eq(1)
  yield
  f.d.until += Const(0)
  yield


def _deadline(m, done, age, deadline):
  with m.If(age >= deadline):
    m.d.comb += Assert(done)
  m.d.comb += Cover(done & (age == deadline))


def _domains():
  m = Module()
  ready, age = _ready(m), _age(m)
  done, active = Signal(), Signal()
  every, once = Signal(8), Signal(8)
  expected_every, expected_once = Signal(8), Signal(8)
  waiting = Signal(init=1)
  with m.If(waiting):
    m.d.sync += expected_every.eq(expected_every + 1)
    with m.If(ready):
      m.d.sync += [waiting.eq(0), expected_once.eq(expected_once + 1)]
  m.d.comb += [Assert(every == expected_every), Assert(once == expected_once),
               Assert(active == waiting), Cover(active & ~ready)]

  def program(f):
    f.d.comb += active.eq(1)
    f.d.sync += every.eq(every + 1)
    f.d.on_done += once.eq(once + 1)
    f.d.until += ready
    yield
    yield from _finish(f, done)

  expand(m, program)
  _deadline(m, done, age, 6)
  return m


def _capture(composite):
  m = Module()
  ready, age = _ready(m), _age(m)
  source = Signal(ArrayLayout(unsigned(8), 2))
  index = AnyConst(1)
  saved, out = Signal(16), Signal(16)
  done = Signal()
  m.d.comb += source.eq(AnySeq(16))
  with m.If(age == 0):
    m.d.sync += saved.eq(source.as_value())
  mask = Const(0xA55A, 16) if composite else Mux(index, 0x5A00, 0x005A)

  def program(f):
    captured = f.capture(source)
    yield
    if composite:
      f.d.on_done += Cat(captured[0], captured[1]).eq(Value.cast(captured) ^ mask)
    else:
      f.d.on_done += captured[index].eq(captured[index] ^ 0x5A)
    f.d.until += ready
    yield
    f.d.sync += out.eq(captured)
    yield
    yield from _finish(f, done)

  expand(m, program)
  with m.If(done):
    m.d.comb += Assert(out == (saved ^ mask))
  m.d.comb += Cover(done & (source.as_value() != saved))
  _deadline(m, done, age, 8)
  return m


def _branch(effects_only):
  m = Module()
  choice, ready, age = AnySeq(1), _ready(m), _age(m)
  done, active, chosen, waiting = Signal(), Signal(), Signal(), Signal(init=1)
  entries, result = Signal(8), Signal(8)
  expected = Signal(8)
  with m.If(age == 0):
    m.d.sync += chosen.eq(choice)
  if effects_only:
    with m.If(waiting & ready):
      m.d.sync += [expected.eq(Mux(choice, 0xA5, 0x5A)), waiting.eq(0)]
  else:
    with m.If(waiting & ready):
      m.d.sync += [expected.eq(Mux(Mux(age == 0, choice, chosen), 0xA5, 0x5A)),
                   waiting.eq(0)]
  m.d.comb += [Assert(result == expected), Assert(active == waiting)]

  def program(f):
    f.d.sync += entries.eq(entries + 1)
    if effects_only:
      f.d.comb += active.eq(1)
      with f.If(choice):
        f.d.on_done += result.eq(0xA5)
      with f.Else():
        f.d.on_done += result.eq(0x5A)
      f.d.until += ready
      yield
    else:
      with f.If(choice):
        f.d.comb += active.eq(1)
        f.d.on_done += result.eq(0xA5)
        f.d.until += ready
        yield
      with f.Else():
        f.d.comb += active.eq(1)
        f.d.on_done += result.eq(0x5A)
        f.d.until += ready
        yield
    yield from _finish(f, done)

  expand(m, program)
  if not effects_only:
    with m.If(age != 0):
      m.d.comb += Assert(entries == 1, "idle prefix must not replay in retry states")
    m.d.comb += Cover(done & (choice != chosen))
  m.d.comb += [Cover(done & (result == 0xA5)), Cover(done & (result == 0x5A))]
  _deadline(m, done, age, 7)
  return m


def _countdown():
  m = Module()
  n, ready, age = AnyConst(3), _ready(m), _age(m)
  m.d.comb += Assume(n <= 4)
  done, active = Signal(), Signal()
  count, expected_count = Signal(4), Signal(4)
  remaining = Signal(3)
  current = Mux(age == 0, n, remaining)
  m.d.sync += remaining.eq(Mux((current != 0) & ready, current - 1, current))
  with m.If((current != 0) & ready):
    m.d.sync += expected_count.eq(expected_count + 1)
  m.d.comb += [Assert(count == expected_count), Assert(active == (current != 0))]

  def program(f):
    left = f.capture(n)
    with f.While(left != 0):
      f.d.comb += active.eq(1)
      f.d.on_done += [left.eq(left - 1), count.eq(count + 1)]
      f.d.until += ready
      yield
    yield from _finish(f, done)

  expand(m, program)
  with m.If(done):
    m.d.comb += Assert(count == n)
  m.d.comb += [Cover(done & (n == 0)), Cover(done & (n == 4)), Cover(active & ~ready)]
  _deadline(m, done, age, 13)
  return m


def _nested_break():
  m = Module()
  n, age = AnyConst(2), _age(m)
  m.d.comb += Assume((n >= 1) & (n <= 3))
  visits, rounds, done = Signal(4), Signal(4), Signal()

  def program(f):
    outer = f.capture(n)
    with f.While(outer != 0):
      inner = f.capture(Const(3, 2))
      with f.While(inner != 0):
        with f.If(inner == 2):
          f.Break()
        f.d.sync += [inner.eq(inner - 1), visits.eq(visits + 1)]
        yield
      f.d.sync += [outer.eq(outer - 1), rounds.eq(rounds + 1)]
      yield
    yield from _finish(f, done)

  expand(m, program)
  m.d.comb += [Assert(rounds <= n), Assert(visits <= n)]
  with m.If(done):
    m.d.comb += [Assert(rounds == n), Assert(visits == n)]
  m.d.comb += [Cover(done & (n == 1)), Cover(done & (n == 3))]
  _deadline(m, done, age, 12)
  return m


def _heap_helper(kind):
  m = Module()
  port = HeapPort().create()
  ready, age = _ready(m), _age(m, 36)
  a, b, data = AnyConst(Addr), AnyConst(Addr), AnyConst(Word)
  replies = Array([AnyConst(Word), AnyConst(Word)])
  if kind == "batch":
    m.d.comb += Assume(a != b)
  count = 2 if kind == "repeat" else 1
  reads, writes, callbacks = Signal(3), Signal(3), Signal(3)
  pending, response = Signal(), Signal()
  sampled, returned = Signal(Word), Signal(Word)
  m.d.comb += [port.granted.eq((port.read | port.write) & ready),
               port.read_data.eq(Mux(response, returned, AnySeq(Word)))]
  m.d.sync += [pending.eq(port.read & port.granted), response.eq(pending)]
  with m.If(port.read & port.granted):
    m.d.sync += [sampled.eq(replies[reads]), reads.eq(reads + 1)]
  with m.If(pending):
    m.d.sync += returned.eq(sampled)
  with m.If(port.write & port.granted):
    m.d.sync += writes.eq(writes + 1)
  with m.If(port.read):
    m.d.comb += Assert(port.read_addr == a)
  with m.If(port.write):
    m.d.comb += [Assert(port.write_addr == (b if kind == "batch" else a)),
                 Assert(port.write_data == data)]
  output = Array([Signal(Word), Signal(Word)])
  done = Signal()

  def program(f):
    i = f.capture(Const(0, 2))
    with f.While(i < count):
      f.d.on_done += callbacks.eq(callbacks + 1)
      if kind == "swap":
        result = yield HeapPort.swap(f, port, addr=a, write=data)
      elif kind == "batch":
        result = HeapPort.read(f, port, addr=a)
        yield HeapPort.write(f, port, addr=b, write=data)
      else:
        result = yield HeapPort.read(f, port, addr=a)
      # Save the response, then use it after live read_data has been poisoned.
      yield
      f.d.sync += [output[i].eq(result), i.eq(i + 1)]
      yield
    yield from _finish(f, done)

  expand(m, program)
  expected_writes = 1 if kind in ("swap", "batch") else 0
  m.d.comb += [Assert(reads <= count), Assert(writes <= expected_writes),
               Assert(callbacks <= count), Cover(port.read & ~port.granted)]
  with m.If(done):
    m.d.comb += [Assert(reads == count), Assert(writes == expected_writes),
                 Assert(callbacks == count)]
    for i in range(count):
      m.d.comb += Assert(output[i] == replies[i])
  _deadline(m, done, age, 24)
  return m


def cases() -> list[FormalCase]:
  return [
    FormalCase("flow_sync_and_on_done", _domains, depth=12, mode="bmc"),
    *[FormalCase(f"flow_capture_{'composite' if composite else 'projection'}",
                 partial(_capture, composite), depth=14, mode="bmc")
      for composite in (False, True)],
    *[FormalCase(f"flow_branch_{'effects' if effects else 'temporal'}",
                 partial(_branch, effects), depth=14, mode="bmc")
      for effects in (False, True)],
    FormalCase("flow_countdown", _countdown, depth=18, mode="bmc"),
    FormalCase("flow_nested_break", _nested_break, depth=18, mode="bmc"),
    *[FormalCase(f"flow_heap_helper_{kind}", partial(_heap_helper, kind),
                 depth=30, mode="bmc") for kind in ("read", "swap", "batch", "repeat")],
  ]
