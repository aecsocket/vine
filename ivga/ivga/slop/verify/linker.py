"""Bounded wire-path semantics of the actual runtime.linker.Linker.

Cutpoints, not a PairAlloc/Heap integration proof: a small sparse word store
models two-edge, read-first HeapPort accesses; a free service marks one live
half-word freed at completion. Partners remain reserved, so no pair/page release
or allocator metadata traffic is modeled. All addresses are legal non-metadata
half-words in the first page. Canary words must remain unchanged.

Both services CAPTURE requests before granting: capture on the first request
edge, then grant after one extra stall (heap), or 0..1 extra stalls (free).
Requests must remain stable through grant. Heap reads sample the OLD word on
the granting edge, and return it only after the following edge; read_data is
arbitrary poison otherwise. Free done is a one-cycle pulse, 0..1 stalls after
grant, independently for each request.
The caller holds Linker operands until req_link & granted, then changes BOTH
packed operands arbitrarily on every cycle. No DUT payload is assumed correct.

Expected heap/free transcripts and final memory are specified independently of
DUT outputs. Every case checks completion exactly once, a finite progress
deadline, exact operation/free counts and order, statistics, absence of faults,
and post-completion quiescence. Value payloads (45 bits), labels (16 bits), and
untouched memory are symbolic constants. Addresses/topologies are deliberately
small and concrete; these are BMC baselines, not induction or arbitrary-graph
termination proofs. Reset, concurrent clients, allocation/coalescing, cycles,
and dangling/invalid wire addresses are excluded. The full-symbolic two-slot
follow is verified with a longer timeout and explicitly marked slow=True.
"""

from dataclasses import dataclass
from functools import partial

from amaranth import Array, Assert, Cat, Const, Cover, Module, Mux, Signal, Value
from amaranth.asserts import AnyConst, AnySeq
from amflow import ValueLike

from ivga import Tag
from ivga.runtime.linker import Linker

from .core import FormalCase

# Distinct live halves, with their partners reserved by an external owner.
TARGET, FIRST, SECOND, TAIL = 2, 4, 6, 8
ADDRESSES = (TARGET, FIRST, SECOND, TAIL, 3, 5, 7, 9)
FREED = Const(0xFFFF_FFFF_FFFF_FFF8, 64)


def _bits(value: ValueLike, width: int) -> Value:
  return Value.cast(Cat(Value.cast(value), Const(0, width))[:width])


def _port(tag: ValueLike, payload: ValueLike = 0, label: ValueLike = 0) -> Value:
  # Independent oracle: do not use the DUT's Port.new/aux helpers.
  return Value.cast(Cat(_bits(tag, 3), _bits(payload, 45), _bits(label, 16)))


def _select(entries, index, default=0):
  return Array(entries or [Const(default, 64)])[index]


def _lookup(cells, address):
  result = Const(0, 64)
  for location, word in cells.items():
    result = Mux(address == location, word, result)
  return result


def _member(address):
  result = Const(0)
  for location in ADDRESSES:
    result = result | (address == location)
  return result


@dataclass
class _HeapOperation:
  address: int
  write: Value | None = None


def _heap(m, port, cells, operations):
  """Public-port protocol model, using captured commands and old memory data."""
  count = Signal(range(len(operations) + 2), name="heap_accept_count")
  writes = Signal(range(len(operations) + 2), name="heap_write_count")
  captured = Signal(name="heap_captured")
  held_read, held_write = Signal(), Signal()
  held_ra, held_wa = Signal(10), Signal(10)
  held_data = Signal(64)
  wait = Signal(2, name="heap_grant_wait")
  desired_delay = Const(1, 2)
  request = port.read | port.write
  grant = captured & (wait == desired_delay)
  took_read = grant & held_read
  took_write = grant & held_write
  sampled, returned = Signal(64), Signal(64)
  pending, response = Signal(), Signal()
  stalled = Signal(name="heap_stalled")
  m.d.comb += [port.granted.eq(grant),
               port.read_data.eq(Mux(response, returned, AnySeq(64))),
               Assert(count <= len(operations))]
  m.d.sync += [pending.eq(took_read), response.eq(pending)]
  with m.If(took_read):
    # This edge samples before either the heap write or a free updates memory.
    m.d.sync += sampled.eq(_lookup(cells, held_ra))
  with m.If(pending):
    m.d.sync += returned.eq(sampled)
  with m.If(request & ~captured):
    m.d.sync += [captured.eq(1), held_read.eq(port.read), held_write.eq(port.write),
                 held_ra.eq(port.read_addr), held_wa.eq(port.write_addr),
                 held_data.eq(port.write_data), wait.eq(0)]
    m.d.comb += [Assert(count < len(operations)), Assert(port.read),
                 Assert(port.read_addr == _select([Const(op.address, 10) for op in operations], count)),
                 Assert(port.write == _select([Const(op.write is not None) for op in operations], count))]
    with m.If(port.write):
      m.d.comb += [Assert(port.write_addr == port.read_addr),
                   Assert(port.write_data == _select(
                     [op.write if op.write is not None else Const(0, 64) for op in operations], count))]
  with m.If(captured):
    m.d.comb += [Assert(request), Assert(port.read == held_read),
                 Assert(port.write == held_write), Assert(port.read_addr == held_ra),
                 Assert(_member(held_ra))]
    with m.If(held_write):
      m.d.comb += [Assert(port.write_addr == held_wa), Assert(port.write_data == held_data),
                   Assert(_member(held_wa))]
    with m.If(grant):
      m.d.sync += [captured.eq(0), count.eq(count + 1)]
    with m.Else():
      m.d.sync += [wait.eq(wait + 1), stalled.eq(1)]
  with m.If(took_write):
    m.d.sync += writes.eq(writes + 1)
  return {"count": count, "writes": writes, "captured": captured, "pending": pending,
          "response": response, "took_write": took_write, "write_addr": held_wa,
          "write_data": held_data, "stalled": stalled}


def _free(m, port, expected):
  """Capture, grant, complete; each transaction gets independent legal delays."""
  accepted = Signal(range(len(expected) + 2), name="free_accept_count")
  completed = Signal(range(len(expected) + 2), name="free_complete_count")
  captured, busy = Signal(name="free_captured"), Signal(name="free_busy")
  address = Signal(10, name="free_address")
  grant_wait, done_wait, latency = Signal(2), Signal(2), Signal(2)
  grant_delays = [AnyConst(1) for _ in expected]
  done_delays = [AnyConst(1) for _ in expected]
  grant = captured & ~busy & (grant_wait == _select(grant_delays, accepted))
  finish = busy & (done_wait == latency)
  stalled, delayed = Signal(name="free_stalled"), Signal(name="free_delayed")
  m.d.comb += [port.granted.eq(grant), port.done.eq(finish), port.oom.eq(0),
               Assert(port.req_alloc == 0), Assert(accepted <= len(expected)),
               Assert(completed <= accepted)]
  with m.If((port.req_free != 0) & ~captured & ~busy):
    m.d.sync += [captured.eq(1), address.eq(port.free_addrs[0]), grant_wait.eq(0)]
    m.d.comb += [Assert(accepted < len(expected)), Assert(port.req_free == 1),
                 Assert(port.free_addrs[0] == _select([Const(a, 10) for a in expected], accepted))]
  with m.If(captured):
    m.d.comb += [Assert(port.req_free == 1), Assert(port.free_addrs[0] == address),
                 Assert(~busy), Assert(_member(address))]
    with m.If(grant):
      m.d.sync += [captured.eq(0), busy.eq(1), accepted.eq(accepted + 1),
                   done_wait.eq(0), latency.eq(_select(done_delays, accepted))]
    with m.Else():
      m.d.sync += [grant_wait.eq(grant_wait + 1), stalled.eq(1)]
  with m.If(busy):
    m.d.comb += [Assert(port.req_free == 0), Assert(port.free_addrs[0] == address)]
    with m.If(finish):
      m.d.sync += [busy.eq(0), completed.eq(completed + 1)]
    with m.Else():
      m.d.sync += [done_wait.eq(done_wait + 1), delayed.eq(1)]
  return {"accepted": accepted, "completed": completed, "captured": captured, "busy": busy,
          "finish": finish, "address": address, "stalled": stalled, "delayed": delayed}


def _active(m, port, expected):
  pushed = Signal(name="active_pushed")
  stalled = Signal()
  delay = AnyConst(1)
  wait = Signal(2)
  grant = port.req_push & ~pushed & (wait == delay)
  m.d.comb += [port.full.eq(0), port.empty.eq(~pushed), port.granted.eq(grant),
               port.done.eq(0), port.pop_data.eq(0), Assert(~port.req_pop)]
  if expected is None:
    m.d.comb += Assert(~port.req_push)
  else:
    with m.If(port.req_push):
      m.d.comb += [Assert(~pushed), Assert(port.push_data.a.as_value() == expected[0]),
                   Assert(port.push_data.b.as_value() == expected[1])]
      with m.If(grant):
        m.d.sync += pushed.eq(1)
      with m.Else():
        m.d.sync += [wait.eq(wait + 1), stalled.eq(1)]
  return pushed, stalled


def _fixture(kind):
  value = _port(Tag.EXT_VAL, AnyConst(45), AnyConst(16))
  previous = _port(Tag.EXT_VAL, AnyConst(45), AnyConst(16))
  initial = {address: (_port(Tag.EXT_VAL, AnyConst(45), AnyConst(16))
                       if address & 1 else AnyConst(64)) for address in ADDRESSES}
  initial[TARGET] = _port(Tag.NONE)
  a, b = _port(Tag.WIRE, TARGET), value
  operations = [_HeapOperation(TARGET, value)]
  frees = []
  final = dict(initial)
  final[TARGET] = value
  stat, active = 0, None
  deadline = 24
  if kind == "self":
    b = a
    operations = [_HeapOperation(TARGET)]
    frees = [TARGET]
    final[TARGET] = FREED
    deadline = 28
  elif kind in ("follow_one", "follow_two", "follow_empty", "follow_self"):
    b = _port(Tag.WIRE, FIRST)
    initial[FIRST] = value
    operations = [_HeapOperation(FIRST), _HeapOperation(TARGET, value)]
    frees = [FIRST]
    final = dict(initial)
    final[FIRST], final[TARGET] = FREED, value
    deadline = 40
    if kind != "follow_one":
      initial[FIRST] = _port(Tag.WIRE, SECOND if kind != "follow_self" else TARGET)
      final[FIRST] = FREED
      if kind == "follow_two":
        initial[SECOND] = value
        final[SECOND] = FREED
        frees = [FIRST, SECOND]
        operations.insert(1, _HeapOperation(SECOND))
      elif kind == "follow_empty":
        initial[SECOND] = _port(Tag.NONE)
        final[SECOND] = initial[SECOND]
        final[TARGET] = _port(Tag.WIRE, SECOND)
        operations = [_HeapOperation(FIRST), _HeapOperation(SECOND),
                      _HeapOperation(TARGET, final[TARGET])]
      else:
        frees = [FIRST, TARGET]
        final[TARGET] = FREED
        operations = [_HeapOperation(FIRST), _HeapOperation(TARGET)]
      deadline = 50
  elif kind in ("occupied_erase", "occupied_tail", "occupied_active"):
    initial[TARGET] = previous
    final[TARGET] = FREED
    frees = [TARGET]
    stat = 1
    deadline = 38
    if kind == "occupied_tail":
      initial[TARGET] = _port(Tag.WIRE, TAIL)
      initial[TAIL] = _port(Tag.NONE)
      final[TAIL] = value
      operations.append(_HeapOperation(TAIL, value))
      stat = 0
      deadline = 45
    elif kind == "occupied_active":
      previous = _port(Tag.COMB, 16, AnyConst(16))
      initial[TARGET] = previous
      active, stat = (previous, value), 0
  elif kind != "empty":
    raise ValueError(kind)
  return a, b, initial, final, operations, frees, stat, active, deadline


def _build(kind, *, reverse=False):
  m = Module()
  dut = m.submodules.linker = Linker()
  a, b, initial, final, operations, frees, stat, active, deadline = _fixture(kind)
  if reverse:
    a, b = b, a
  age = Signal(range(deadline + 5), name="age")
  with m.If(age < deadline + 4):
    m.d.sync += age.eq(age + 1)
  started = Signal(name="command_accepted")
  changed = Signal(name="caller_changed")
  # Initial memory is established at edge zero. Request and payload are then
  # held until the real controller grants, never changed on the capture edge.
  m.d.comb += [dut.req_link.eq((age != 0) & ~started),
               dut.req_a.eq(Mux(started, AnySeq(64), a)),
               dut.req_b.eq(Mux(started, AnySeq(64), b)), dut.stat_reset.eq(0),
               dut.fault.halt.eq(0)]
  with m.If(dut.req_link & dut.granted):
    m.d.comb += Assert(~started)
    m.d.sync += started.eq(1)
  with m.If(started & ((dut.req_a.as_value() != a) | (dut.req_b.as_value() != b))):
    m.d.sync += changed.eq(1)
  cells = {address: Signal(64, name=f"word_{address}") for address in ADDRESSES}
  heap = _heap(m, dut.heap, cells, operations)
  free = _free(m, dut.alloc, frees)
  pushed, push_stalled = _active(m, dut.active, active)
  freed = Signal(len(ADDRESSES), name="freed_words")
  for index, (address, cell) in enumerate(cells.items()):
    with m.If(age == 0):
      m.d.sync += cell.eq(initial[address])
    with m.Else():
      with m.If(heap["took_write"] & (heap["write_addr"] == address)):
        m.d.sync += cell.eq(heap["write_data"])
      with m.If(free["finish"] & (free["address"] == address)):
        m.d.comb += [Assert(freed[index] == 0), Assert(~heap["took_write"])]
        m.d.sync += [cell.eq(FREED), freed[index].eq(1)]
    # Unaddressed words, including reserved partners, must never be modified.
    if address not in {op.address for op in operations if op.write is not None} | set(frees):
      with m.If(age != 0):
        m.d.comb += Assert(cell == initial[address])
  with m.If(~started):
    m.d.comb += [Assert(~dut.heap.read), Assert(~dut.heap.write),
                 Assert(dut.alloc.req_free == 0), Assert(~dut.active.req_push)]
  done_seen = Signal(name="done_seen")
  with m.If(dut.done):
    m.d.comb += [Assert(started), Assert(~done_seen)]
    m.d.sync += done_seen.eq(1)
  m.d.comb += Assert(dut.stat_erase == Mux(done_seen | dut.done, stat, 0))
  for name in ("oom", "bad_graft", "active_full", "ext_copy", "ext_inputs", "ext_generic",
               "invalid_interaction"):
    m.d.comb += Assert(~getattr(dut.fault, name))
  with m.If(dut.done | done_seen):
    m.d.comb += [Assert(heap["count"] == len(operations)),
                 Assert(heap["writes"] == sum(op.write is not None for op in operations)),
                 Assert(free["accepted"] == len(frees)), Assert(free["completed"] == len(frees)),
                 Assert(freed == sum(1 << ADDRESSES.index(address) for address in frees)),
                 Assert(pushed == (active is not None)), Assert(~heap["captured"]),
                 Assert(~heap["pending"]), Assert(~free["busy"]), Assert(~free["captured"]),
                 Assert(~dut.heap.read), Assert(~dut.heap.write), Assert(dut.alloc.req_free == 0),
                 Assert(~dut.active.req_push)]
    for address, expected in final.items():
      m.d.comb += Assert(cells[address] == expected)
  with m.If(age >= deadline):
    m.d.comb += Assert(done_seen)
  m.d.comb += [Cover(done_seen & (age == deadline) & changed),
               Cover(done_seen & (age == deadline + 3))]
  if operations:
    m.d.comb += Cover(done_seen & heap["stalled"])
  if frees:
    m.d.comb += Cover(done_seen & free["stalled"] & free["delayed"])
  if active is not None:
    m.d.comb += Cover(done_seen & push_stalled)
  written = [op.write for op in operations if op.write is not None]
  if written and kind != "follow_empty":
    m.d.comb += Cover(done_seen & (_bits(written[0] >> 3, 45) == (1 << 45) - 1) &
                         (_bits(written[0] >> 48, 16) == 0xFFFF))
  if kind == "follow_empty":
    m.d.comb += Cover(done_seen & (cells[SECOND] == _port(Tag.NONE)) &
                     (cells[TARGET] == _port(Tag.WIRE, SECOND)))
  return m


def cases() -> list[FormalCase]:
  """Small, explicit bounds; no sequential result is advertised as induction."""
  result = []
  for kind, reverse in (("self", False), ("empty", False), ("empty", True),
                        ("follow_one", False), ("follow_two", False),
                        ("follow_empty", False), ("follow_self", False),
                        ("occupied_erase", False), ("occupied_tail", False),
                        ("occupied_active", False)):
    slow = kind == "follow_two"
    result.append(FormalCase(f"linker_wire_{kind}{'_reverse' if reverse else ''}",
                             partial(_build, kind, reverse=reverse), depth=64, mode="bmc",
                             timeout=120 if slow else 30, slow=slow))
  return result
