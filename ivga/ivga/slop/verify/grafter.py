"""Bounded header-rejection contracts for the actual Grafter controller.

A protocol-level heap returns one symbolic header with the production two-edge
read latency. Caller operands may change arbitrarily after acceptance, and read
data is poisoned outside the response window. No allocations/links should occur
for the rejected header/root. This is not a valid-graft interpreter proof.
"""

from functools import partial

from amaranth import Assert, Assume, Cat, Const, Cover, Module, Mux, Signal
from amaranth.asserts import AnyConst, AnySeq

from ivga import Addr, Port, Word
from ivga.mem.page_alloc import PAGE_SHIFT, PAGE_SIZE
from ivga.runtime.grafter import MAX_REGISTERS, Grafter

from .core import FormalCase


def _reject(kind):
  m = Module()
  dut = m.submodules.dut = Grafter()
  count = Const(1, 31)
  error = Const(0, 1)
  root = Port.eraser().as_value()
  addr = AnyConst(Addr)
  if kind == "zero_registers":
    count = Const(0, 31)
  elif kind == "too_many_registers":
    count = AnyConst(31)
    m.d.comb += Assume(count > MAX_REGISTERS)
  elif kind == "error_header":
    error = Const(1, 1)
  elif kind == "none_root":
    root = Port.none().as_value()
  elif kind == "footer_entry":
    m.d.comb += Assume(addr[:PAGE_SHIFT] == PAGE_SIZE - 1)
  else:
    raise ValueError(kind)
  if kind != "footer_entry":
    m.d.comb += Assume(addr[:PAGE_SHIFT] != PAGE_SIZE - 1)
  header = Cat(count, error, AnyConst(32))

  age = Signal(range(25))
  with m.If(age < 24):
    m.d.sync += age.eq(age + 1)
  accepted, completed = Signal(), Signal()
  with m.If(dut.req_graft & dut.granted):
    m.d.sync += [Assert(~accepted), accepted.eq(1)]
  m.d.comb += [dut.req_graft.eq(~accepted),
               dut.req_addr.eq(Mux(accepted, AnySeq(Addr), addr)),
               dut.req_port.eq(Mux(accepted, AnySeq(Word), root))]

  ready = AnySeq(1)
  denied = Signal()
  m.d.comb += Assume(~denied | ready)
  m.d.sync += denied.eq(dut.heap.read & ~ready)
  m.d.comb += dut.heap.granted.eq(dut.heap.read & ready)
  pending, valid, issued = Signal(), Signal(), Signal(2)
  sample, data = Signal(Word), Signal(Word)
  m.d.sync += [pending.eq(dut.heap.read & dut.heap.granted), valid.eq(pending)]
  with m.If(dut.heap.read & dut.heap.granted):
    m.d.sync += [sample.eq(header), issued.eq(issued + 1)]
  with m.If(pending):
    m.d.sync += data.eq(sample)
  m.d.comb += dut.heap.read_data.eq(Mux(valid, data, AnySeq(Word)))
  with m.If(dut.heap.read):
    m.d.comb += Assert(dut.heap.read_addr == addr)

  m.d.comb += [Assert(issued <= 1), Assert(~dut.heap.write),
               Assert(dut.pages.req_alloc == 0), Assert(dut.pages.req_free == 0),
               Assert(dut.pairs.req_alloc == 0), Assert(dut.pairs.req_free == 0),
               Assert(~dut.linker.req_link),
               dut.pages.granted.eq(0), dut.pages.done.eq(0), dut.pages.oom.eq(0),
               dut.pages.alloc_ids.eq(0), dut.pairs.granted.eq(0), dut.pairs.done.eq(0),
               dut.pairs.oom.eq(0), dut.pairs.alloc_bases.eq(0),
               dut.linker.granted.eq(0), dut.linker.done.eq(0)]
  with m.If(dut.done):
    m.d.comb += [Assert(accepted), Assert(~completed), Assert(dut.error),
                 Assert(~dut.oom), Assert(issued == 1)]
    m.d.sync += completed.eq(1)
  with m.If(age >= 18):
    m.d.comb += Assert(completed, "invalid graft must report completion/error")
  m.d.comb += [Cover(completed & (age == 18)),
               Cover(completed & (dut.req_addr != addr))]
  return m


def cases() -> list[FormalCase]:
  return [FormalCase(f"grafter_reject_{kind}", partial(_reject, kind), depth=24, mode="bmc")
          for kind in ("zero_registers", "too_many_registers", "error_header",
                       "none_root", "footer_entry")]
