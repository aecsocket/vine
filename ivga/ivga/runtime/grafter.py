# SLOP
"""Instantiate page-backed net templates.

A Graft word is followed by its instruction stream. Binary instructions occupy
one word; nilary instructions occupy an Instruction word followed by a packed
Port. The last word of each source page holds the next page ID (not a word
address), and is skipped by the instruction reader. Source pages must already
be reserved by the loader and remain immutable during construction.

Register indices are local to one instantiation, with ROOT = 0. Scratch pages
are allocated, initialized, and released for each request. Zero means an empty
register, so NONE ports are not valid instruction endpoints. Node words are
initialized before their ports are exposed to the linker.

OOM/error completes the request and releases scratch pages. Construction is not
transactional: already-created nodes and links are not rolled back on failure;
the caller must stop or discard that execution context.
"""

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout, Struct
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike, expand
from ivga.mem.heap import HeapPort
from ivga.mem.page_alloc import PAGE_SHIFT, PAGE_SIZE, PageAllocPort, PageId
from ivga.mem.pair_alloc import PairAllocPort
from ivga.runtime.linker import LinkerPort

from ivga import WORD_WIDTH, Addr, Port, Tag, flatten

Register = unsigned(Addr.width)
ROOT = 0
MAX_REGISTERS = 1 << Register.width
MAX_REGISTER_PAGES = 1 << PageId.width
_INSTRUCTION_BITS = 1 + 3 + 16 + Register.width * 3
assert _INSTRUCTION_BITS <= WORD_WIDTH


class Graft(Struct):
  next_register: unsigned(31)
  error: unsigned(1)
  instructions_len: unsigned(32)


class Instruction(Struct):
  binary: unsigned(1)
  tag: Tag
  label: unsigned(16)
  r0: Register
  r1: Register
  r2: Register
  padding: unsigned(WORD_WIDTH - _INSTRUCTION_BITS)


class GrafterPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_graft": Out(1),
      "req_addr": Out(Addr),
      "req_port": Out(Port),
      "granted": In(1),
      "done": In(1),
      "oom": In(1),
      "error": In(1),
    })

  @staticmethod
  def graft(f: Flow, grafter, *, addr: ValueLike, port: ValueLike):
    accepted = Signal()
    oom, error = Signal(), Signal()
    f.d.comb += [
      grafter.req_graft.eq(~accepted),
      grafter.req_addr.eq(addr),
      grafter.req_port.eq(port),
    ]
    f.d.sync += accepted.eq(accepted | grafter.granted)
    f.d.until += accepted & grafter.done
    f.d.on_done += [
      accepted.eq(False),
      oom.eq(grafter.oom),
      error.eq(grafter.error),
    ]
    return oom, error


class Grafter(wiring.Component):
  control: In(GrafterPort())
  heap: Out(HeapPort())
  pages: Out(PageAllocPort())
  pairs: Out(PairAllocPort())
  linker: Out(LinkerPort())

  def __init__(self):
    self.register_pages = Signal(ArrayLayout(PageId, MAX_REGISTER_PAGES))
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += self.done.eq(False)
    expand(m, lambda f: program(f, self))
    return m


def fail(f: Flow, grafter: Grafter, failed, *, oom=False):
  f.d.sync += failed.eq(True)
  f.d.sync += grafter.oom.eq(True) if oom else grafter.error.eq(True)
  yield


def register_address(grafter: Grafter, register):
  register = Value.cast(register)
  page = Value.cast(grafter.register_pages[register >> PAGE_SHIFT])
  return (page << PAGE_SHIFT) + (register & (PAGE_SIZE - 1))


def read_word(f: Flow, grafter: Grafter, cursor):
  with f.If(cursor[:PAGE_SHIFT] == PAGE_SIZE - 1):
    next_page = yield HeapPort.read(f, grafter.heap, addr=cursor)
    f.d.sync += [
      Assert(next_page < MAX_REGISTER_PAGES, "Invalid graft continuation page"),
      cursor.eq(next_page << PAGE_SHIFT),
    ]
    yield
  f.d.on_done += cursor.eq(cursor + 1)
  word = yield HeapPort.read(f, grafter.heap, addr=cursor)
  return word


def link_register(f: Flow, grafter: Grafter, register, port, count, pending):
  f.d.sync += Assert(Value.cast(register) < count, "Graft register index out of bounds")
  addr = register_address(grafter, register)
  previous = yield HeapPort.swap(f, grafter.heap, addr=addr, write=0)
  with f.If(previous == 0):
    f.d.on_done += pending.eq(pending + 1)
    yield HeapPort.write(f, grafter.heap, addr=addr, write=port)
  with f.Else():
    f.d.on_done += pending.eq(pending - 1)
    yield LinkerPort.link(f, grafter.linker, a=previous, b=port)


def binary(f: Flow, grafter: Grafter, instruction, count, pending, failed):
  valid = (
    ((instruction.tag == Tag.COMB) | (instruction.tag == Tag.EXT_FN))
    & (instruction.r0 < count)
    & (instruction.r1 < count)
    & (instruction.r2 < count)
  )
  with f.If(~valid):
    yield from fail(f, grafter, failed)
  with f.Else():
    bases, oom = yield PairAllocPort.alloc(f, grafter.pairs, n_pairs=1)
    with f.If(oom):
      yield from fail(f, grafter, failed, oom=True)
    with f.Else():
      base = f.capture(bases[0])
      yield HeapPort.write(f, grafter.heap, addr=base, write=0)
      yield HeapPort.write(f, grafter.heap, addr=Addr.other_half(base), write=0)
      principal = Port.new(instruction.tag, instruction.label, Value.cast(base))
      left, right = Port.aux(principal)
      yield from link_register(f, grafter, instruction.r0, principal, count, pending)
      yield from link_register(f, grafter, instruction.r1, left, count, pending)
      yield from link_register(f, grafter, instruction.r2, right, count, pending)


def nilary(f: Flow, grafter: Grafter, instruction, count, pending, failed, cursor):
  raw = yield from read_word(f, grafter, cursor)
  port = Port(raw)
  valid = (
    ((port.tag == Tag.GRAFT) | (port.tag == Tag.ERASE) | (port.tag == Tag.EXT_VAL))
    & (instruction.r0 < count)
  )
  with f.If(~valid):
    yield from fail(f, grafter, failed)
  with f.Else():
    yield from link_register(f, grafter, instruction.r0, port, count, pending)


def program(f: Flow, grafter: Grafter):
  f.d.comb += grafter.granted.eq(True)
  with f.If(grafter.req_graft):
    addr = f.capture(grafter.req_addr)
    port = f.capture(grafter.req_port)
    failed = f.capture(False)
    allocated = f.capture(Const(0, range(MAX_REGISTER_PAGES + 1)))
    f.d.sync += [grafter.oom.eq(False), grafter.error.eq(False)]
    raw = yield HeapPort.read(f, grafter.heap, addr=addr)
    header = Graft(raw)
    valid = (
      ~header.error
      & (header.next_register >= 1)
      & (header.next_register <= MAX_REGISTERS)
      & (addr[:PAGE_SHIFT] != PAGE_SIZE - 1)
      & (port.tag != Tag.NONE)
    )
    with f.If(~valid):
      yield from fail(f, grafter, failed)
    with f.Else():
      needed = (header.next_register + PAGE_SIZE - 1) >> PAGE_SHIFT
      with f.While((allocated < needed) & ~failed):
        page_ids, oom = yield PageAllocPort.alloc(f, grafter.pages, n_pages=1)
        with f.If(oom):
          yield from fail(f, grafter, failed, oom=True)
        with f.Else():
          f.d.sync += [
            grafter.register_pages[allocated].eq(page_ids[0]),
            allocated.eq(allocated + 1),
          ]
          yield

      with f.If(~failed):
        index = f.capture(Const(0, unsigned(Register.width + 1)))
        with f.While(index < header.next_register):
          f.d.on_done += index.eq(index + 1)
          yield HeapPort.write(
            f, grafter.heap, addr=register_address(grafter, index),
            write=Mux(index == ROOT, port, 0),
          )

        cursor = f.capture(addr + 1)
        remaining = f.capture(header.instructions_len)
        pending = f.capture(Const(1, range(MAX_REGISTERS + 1)))
        with f.While((remaining != 0) & ~failed):
          raw_instruction = yield from read_word(f, grafter, cursor)
          instruction = Instruction(raw_instruction)
          f.d.on_done += remaining.eq(remaining - 1)
          with f.If(instruction.binary):
            yield from binary(f, grafter, instruction, header.next_register, pending, failed)
          with f.Else():
            yield from nilary(f, grafter, instruction, header.next_register, pending, failed, cursor)

        with f.If(~failed & (pending != 0)):
          yield from fail(f, grafter, failed)

    with f.While(allocated != 0):
      page = Value.cast(grafter.register_pages[(allocated - 1).as_unsigned()])
      f.d.on_done += allocated.eq(allocated - 1)
      yield PageAllocPort.free(f, grafter.pages, n_pages=1, page_ids=page)
    f.d.sync += grafter.done.eq(True)
    yield
