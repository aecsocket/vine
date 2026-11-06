"""Direct handler tests: python -B -m ivga.slop.test_extrinsic_handlers.

The heap model grants each held request once and supplies its synchronous
response for one cycle after the next edge (two-edge latency). Read data is
poisoned otherwise. No files or waveforms are produced.
"""

import operator
import random
import unittest
from dataclasses import dataclass

from amaranth import Const, Module, Signal
from amaranth.sim import Simulator
from amflow import expand

from ivga0 import ADDR_MAX, Port, Tag
from ivga0.extrinsic import branch, n32
from ivga0.extrinsic.common import ExtFnContext
from ivga0.mem.heap import CAPACITY, HeapPort
from ivga0.mem.page_alloc import PAGE_SIZE
from ivga0.runtime.linker import LinkerPort

MASK32 = 0xFFFF_FFFF
# Deliberately not the registry's default IDs: handlers must use cx.ty_id.
TYPE_IDS = {n32.N32: 0xB731, branch.BRANCH: 0x6ACE}
OPERATIONS = {
  "vi:n32:add": operator.add,
  "vi:n32:sub": operator.sub,
  "vi:n32:eq": operator.eq,
  "vi:n32:ne": operator.ne,
}


def packed(tag, payload=0, label=0):
  return Const({"tag": tag, "payload": payload, "label": label}, Port).as_value().value


ERASER = packed(Tag.ERASE)
POISON = packed(Tag.EXT_FN, (1 << 45) - 1, 0xDEAD)


@dataclass(frozen=True)
class Case:
  a: int
  b: int
  out: int
  result: int
  reads: tuple[int, ...] = ()
  error: bool = False


@dataclass
class Pending:
  request: tuple[int, int]
  remaining: int


class Harness:
  def __init__(self, entry):
    self.module = m = Module()
    self.start = Signal()
    self.done = Signal()
    self.prime_error = Signal()
    self.a, self.b, self.out = (
      Port(Signal(Port.as_shape().size, name=name)) for name in ("a", "b", "out")
    )
    self.heap = HeapPort().create()
    self.linker = LinkerPort().create()
    m.d.sync += self.done.eq(0)

    def program(f):
      self.cx = cx = ExtFnContext(f, self.heap, self.linker, self.a, self.b, self.out)
      cx.ty_id.update(TYPE_IDS)
      with f.If(self.start):
        yield from entry.fn(cx)
        f.d.sync += self.done.eq(1)
        yield

    expand(m, program)
    # N32 has no error path of its own; seed a previous error without changing
    # the handler, then verify its successful completion explicitly clears it.
    with m.If(self.prime_error):
      m.d.sync += self.cx.error.eq(1)


class Services:
  """Bounded, edge-driven HeapPort and LinkerPort protocol checker."""

  def __init__(self, test, dut, memory, delay):
    self.test, self.dut = test, dut
    self.memory = dict(memory)
    self.initial = dict(memory)
    self.delay = delay
    self.heap_pending: Pending | None = None
    self.heap_response: int | None = None
    self.link_pending: Pending | None = None
    self.link_busy: Pending | None = None
    self.reads = []
    self.links = []
    self.link_completions = 0
    self.completions = 0

  async def step(self, ctx):
    t, d = self.test, self.dut
    h, link = d.heap, d.linker
    ctx.set(h.granted, 0)
    ctx.set(link.granted, 0)
    ctx.set(link.done, 0)
    t.assertFalse(ctx.get(h.write), "handler must not modify the table")
    response = self.heap_response
    self.heap_response = None
    if response is not None:
      t.assertFalse(ctx.get(h.read), "accepted heap read was requested again while awaiting response")

    request = (ctx.get(h.read), ctx.get(h.read_addr))
    if self.heap_pending is not None:
      t.assertEqual(request, self.heap_pending.request, "heap request changed before grant")
    if request[0]:
      if self.heap_pending is None:
        self.heap_pending = Pending(request, self.delay)
      if self.heap_pending.remaining:
        self.heap_pending.remaining -= 1
      else:
        addr = request[1]
        t.assertTrue(0 <= addr < CAPACITY, "out-of-heap read")
        t.assertIn(addr, self.memory, "read outside a constructed table")
        self.reads.append(addr)
        self.heap_response = self.memory[addr]
        ctx.set(h.granted, 1)
        self.heap_pending = None

    pair = (ctx.get(link.req_a.as_value()), ctx.get(link.req_b.as_value()))
    if self.link_busy is not None:
      t.assertFalse(ctx.get(link.req_link), "accepted link was requested again")
      t.assertEqual(pair, self.link_busy.request, "link operands changed before done")
      if self.link_busy.remaining:
        self.link_busy.remaining -= 1
      else:
        ctx.set(link.done, 1)
        self.link_completions += 1
        self.link_busy = None
    elif self.link_pending is not None or ctx.get(link.req_link):
      t.assertTrue(ctx.get(link.req_link), "link request withdrawn before grant")
      if self.link_pending is None:
        self.link_pending = Pending(pair, self.delay)
      t.assertEqual(pair, self.link_pending.request, "link operands changed before grant")
      if self.link_pending.remaining:
        self.link_pending.remaining -= 1
      else:
        self.links.append(pair)
        ctx.set(link.granted, 1)
        self.link_busy = Pending(pair, self.delay + 1)
        self.link_pending = None

    if ctx.get(d.done):
      self.completions += 1
      t.assertEqual(self.completions, self.link_completions,
                    "handler completed early or more than once")
    await ctx.tick()
    # A grant at edge N produces a response only after edge N+1.
    # Poison both the request cycle and the first cycle after acceptance.
    ctx.set(h.read_data, POISON if response is None else response)


class ExtrinsicHandlerTests(unittest.TestCase):
  def run_cases(self, entry, cases, memory=None, *, poison_inputs=False, prime_error=False):
    for delay in (1, 4):
      with self.subTest(handler=entry.name, delay=delay):
        dut = Harness(entry)
        service = Services(self, dut, memory or {}, delay)
        sim = Simulator(dut.module)
        sim.add_clock(1e-6)

        async def bench(ctx, dut=dut, service=service):
          ctx.set(dut.heap.read_data, POISON)
          if prime_error:
            ctx.set(dut.prime_error, 1)
            await service.step(ctx)
            ctx.set(dut.prime_error, 0)
            self.assertEqual(ctx.get(dut.cx.error), 1)
          for number, case in enumerate(cases):
            with self.subTest(case=number, a=hex(case.a), b=hex(case.b)):
              reads_before = len(service.reads)
              links_before = len(service.links)
              done_before = service.completions
              ctx.set(dut.a.as_value(), case.a)
              ctx.set(dut.b.as_value(), case.b)
              ctx.set(dut.out.as_value(), case.out)
              ctx.set(dut.start, 1)
              await service.step(ctx)
              ctx.set(dut.start, 0)
              if poison_inputs:
                # Branch captures all three operands at its first edge, even
                # when the count read has not yet been granted.
                for value in (dut.a, dut.b, dut.out):
                  ctx.set(value.as_value(), POISON)
              # N32 operands deliberately remain stable: its caller captures
              # them, and direct-handler input poisoning is not its contract.
              for _ in range(120):
                if ctx.get(dut.done):
                  break
                await service.step(ctx)
              else:
                self.fail("handler did not complete within 120 cycles")
              self.assertEqual(ctx.get(dut.cx.error), int(case.error))
              self.assertEqual(service.reads[reads_before:], list(case.reads))
              self.assertEqual(service.links[links_before:], [(case.out, case.result)])
              # Count the completion pulse and expose replayed requests.
              for _ in range(5):
                await service.step(ctx)
                self.assertEqual(ctx.get(dut.done), 0, "repeated completion")
                self.assertEqual(ctx.get(dut.heap.read), 0)
                self.assertEqual(ctx.get(dut.linker.req_link), 0)
              self.assertEqual(service.completions, done_before + 1)
              self.assertEqual(service.link_completions, service.completions)
              self.assertEqual(len(service.links), links_before + 1)
              self.assertEqual(service.reads[reads_before:], list(case.reads))
              self.assertEqual(service.memory, service.initial, "table changed")
              self.assertIsNone(service.heap_pending)
              self.assertIsNone(service.heap_response)
              self.assertIsNone(service.link_pending)
              self.assertIsNone(service.link_busy)

        sim.add_testbench(bench)
        sim.run()

  def test_arithmetic_registry_coverage(self):
    names = [entry.name for entry in n32.EXT_FNS]
    self.assertEqual(len(names), len(set(names)), "duplicate arithmetic registration")
    self.assertEqual(set(names), set(OPERATIONS),
                     "update arithmetic oracles when an operation is added")
    for entry in n32.EXT_FNS:
      self.assertEqual(entry.inputs, (n32.N32, n32.N32))

  def test_all_n32_arithmetic_boundaries_and_seeded_values(self):
    boundaries = (0, 1, 2, 0x7FFF_FFFE, 0x7FFF_FFFF,
                  0x8000_0000, 0xFFFF_FFFE, MASK32)
    pairs = [(a, b) for a in boundaries for b in boundaries]
    rng = random.Random(0x32ADD5)
    pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(48)]
    # Random equality cases ensure EQ/NE are not only tested on unequal data.
    pairs += [(a, a) for a in (rng.getrandbits(32) for _ in range(24))]
    for entry in n32.EXT_FNS:
      with self.subTest(operation=entry.name):
        self.assertIn(entry.name, OPERATIONS)
        oracle = OPERATIONS[entry.name]
        cases = [
          Case(packed(Tag.EXT_VAL, a, TYPE_IDS[n32.N32]),
               packed(Tag.EXT_VAL, b, TYPE_IDS[n32.N32]),
               packed(Tag.WIRE, 17 + i),
               # Exact packed equality also checks unsigned encoding, 32-bit
               # wrap, zero extension through bit 44, tag and remapped label.
               packed(Tag.EXT_VAL, int(oracle(a, b)) & MASK32, TYPE_IDS[n32.N32]))
          for i, (a, b) in enumerate(pairs)
        ]
        self.run_cases(entry, cases, prime_error=True)

  def branch_cases(self, base, entries, indices):
    self.assertGreaterEqual(base, 0)
    self.assertLess(base + len(entries), CAPACITY)
    # One contiguous allocation: raw word count followed by *packed* ports.
    memory = {base: len(entries)}
    memory.update({base + 1 + i: value for i, value in enumerate(entries)})
    cases = []
    for i, index in enumerate(indices):
      valid = index < len(entries)
      reads = (base, base + 1 + index) if valid else (base,)
      cases.append(Case(
        packed(Tag.EXT_VAL, index, TYPE_IDS[n32.N32]),
        packed(Tag.EXT_VAL, base, TYPE_IDS[branch.BRANCH]),
        packed(Tag.WIRE, 31 + i),
        entries[index] if valid else ERASER,
        reads, not valid,
      ))
    return memory, cases

  def test_branch_first_last_out_of_range_and_error_recovery(self):
    entries = [packed(Tag.GRAFT, payload) for payload in (7, 82, 511, 1000)]
    memory, cases = self.branch_cases(23, entries, (4, 0, 3, MASK32, 1, 5, 0))
    self.run_cases(branch.EXT_FNS[0], cases, memory, poison_inputs=True)

  def test_branch_empty_table(self):
    memory, cases = self.branch_cases(41, [], (0, 1, MASK32))
    self.run_cases(branch.EXT_FNS[0], cases, memory, poison_inputs=True)

  def test_branch_contiguous_table_crosses_pages(self):
    base = PAGE_SIZE - 2
    entries = [packed(Tag.GRAFT, (i * 37 + 9) % CAPACITY)
               for i in range(PAGE_SIZE + 5)]
    memory, cases = self.branch_cases(
      base, entries, (len(entries), 0, 1, 2, PAGE_SIZE, len(entries) - 1))
    self.assertGreater((base + len(entries)) // PAGE_SIZE, base // PAGE_SIZE)
    self.run_cases(branch.EXT_FNS[0], cases, memory, poison_inputs=True)

  def test_branch_last_entry_at_heap_upper_bound(self):
    entries = [packed(Tag.GRAFT, 91), packed(Tag.GRAFT, ADDR_MAX)]
    base = CAPACITY - len(entries) - 1
    memory, cases = self.branch_cases(base, entries, (2, 1, 0, MASK32, 1))
    self.assertEqual(base + len(entries), CAPACITY - 1)
    self.run_cases(branch.EXT_FNS[0], cases, memory, poison_inputs=True)

  def test_branch_empty_table_at_last_heap_word(self):
    memory, cases = self.branch_cases(CAPACITY - 1, [], (0, MASK32))
    self.run_cases(branch.EXT_FNS[0], cases, memory, poison_inputs=True)

  def test_branch_current_payload_checks_skip_all_heap_reads(self):
    memory, success = self.branch_cases(19, [packed(Tag.GRAFT, 87)], (0,))
    bad_payloads = ((1 << 32, 19), ((1 << 45) - 1, 19),
                    (0, ADDR_MAX + 1), (0, (1 << 45) - 1))
    cases = []
    for index, base in bad_payloads:
      cases.append(Case(
        packed(Tag.EXT_VAL, index, TYPE_IDS[n32.N32]),
        packed(Tag.EXT_VAL, base, TYPE_IDS[branch.BRANCH]),
        packed(Tag.WIRE, 79), ERASER, (), True,
      ))
      cases.extend(success)  # A real error followed by success must clear cx.error.
    self.run_cases(branch.EXT_FNS[0], cases, memory, poison_inputs=True)


if __name__ == "__main__":
  _ = unittest.main()
