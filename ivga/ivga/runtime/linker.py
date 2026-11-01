from collections.abc import Iterable

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike, expand
from ivga.mem.heap import HeapPort
from ivga.mem.pair_alloc import AllocPort
from ivga.mem.stack import StackPort

from ivga import Port, Port2, Stat, Tag, flatten

from .fault import FaultPort


class LinkerPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_link": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "granted": In(1),
      "done": In(1),
      # stats
      "stat_erase": In(Stat),
      "stat_reset": Out(1),
    })

  @staticmethod
  def link(f: Flow, linker, *, a: ValueLike, b: ValueLike):
    accepted = Signal()
    f.d.comb += linker.req_link.eq(~accepted)
    f.d.sync += accepted.eq(accepted | linker.granted)
    f.d.until += accepted & linker.done
    f.d.on_done += accepted.eq(False)

  @staticmethod
  def link_batch(f: Flow, linkers: Iterable[tuple["Linker", ValueLike, ValueLike]]):
    for linker, a, b in linkers:
      accepted = Signal()
      was_done = Signal()
      done_now = accepted & linker.done
      f.d.comb += [
        linker.req_link.eq(~accepted),
        linker.req_a.eq(a),
        linker.req_b.eq(b),
      ]
      f.d.sync += [
        accepted.eq(accepted | linker.granted),
        was_done.eq(was_done | done_now),
      ]
      f.d.until += was_done | done_now
      f.d.on_done += [
        accepted.eq(False),
        was_done.eq(False)
      ]


class Linker(wiring.Component):
  control: In(LinkerPort())
  heap: Out(HeapPort())
  alloc: Out(AllocPort())
  active: Out(StackPort(Port2))
  fault: Out(FaultPort())

  def __init__(self):
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m, linker = Module(), self
    m.d.sync += linker.done.eq(False)
    expand(m, lambda f: program(f, linker))

    with m.If(linker.stat_reset):
      m.d.sync += linker.stat_erase.eq(0)

    return m

def program(f: Flow, linker: Linker):
  f.d.comb += linker.granted.eq(True)

  with f.If(linker.req_link):
    a = f.capture(linker.req_a)
    b = f.capture(linker.req_b)

    with f.While(True):
      yield from do_link(f, linker, a, b)

def do_link(f: Flow, linker: Linker, a, b):
  with f.If(a.tag == Tag.WIRE):
    yield from link_wire(f, linker, a, b, next_a=a, next_b=b)

  with f.Elif(b.tag == Tag.WIRE):
    yield from link_wire(f, linker, b, a, next_a=a, next_b=b)

  with f.Elif(
    ((a.tag == Tag.GRAFT) | (a.tag == Tag.ERASE))
    & ((b.tag == Tag.GRAFT) | (b.tag == Tag.ERASE))
  ):
    # (graft|erase) >-< (graft|erase) erases each other;
    # nothing to do, since a graft is a closed net where the only port open
    # to the outside world *is* the one we're looking at right now.
    # so there's no other way for the graft to interact with the rest of the net
    # therefore it's safe to erase it
    f.d.sync += [
      linker.done.eq(True),
      linker.stat_erase.eq(linker.stat_erase + 1),
    ]
    yield
    f.Break()

  with f.Elif(
    ((a.tag == Tag.EXT_VAL) | (a.tag == Tag.ERASE))
    & ((b.tag == Tag.EXT_VAL) | (b.tag == Tag.ERASE))
  ):
    # (extval|erase) >-< (extval|erase) - erasing an ext val is legal,
    # but might be a bad idea (e.g. erasing the IO handle).
    # TODO: add faults for doing bad stuff.
    f.d.sync += [
      linker.done.eq(True),
      linker.stat_erase.eq(linker.stat_erase + 1),
    ]
    yield
    f.Break()

  with f.Else():
    # all other work goes on the worklist
    # TODO: active_fast/active_slow like in IVM
    with f.If(linker.active.full):
      # fault if active worklist is full
      f.d.comb += linker.fault.active_full.eq(True)
      f.d.sync += linker.done.eq(True)
      yield
      f.Break()
    with f.Else():
      active_pair = Signal(Port2)
      f.d.comb += [
        active_pair.a.eq(a),
        active_pair.b.eq(b),
      ]
      yield StackPort.push(f, linker.active, data=active_pair)
      f.Break()

def link_wire(f: Flow, linker: Linker, wire, other, *, next_a, next_b):
  other = yield from follow(f, linker, other)
  with f.If(wire == other):
    # A's bits compose a wire, and B has the same bits,
    # so B is the same wire as A.
    # so we just free `A.payload`, and don't do any real connecting.
    f.d.on_done += linker.done.eq(True)
    yield AllocPort.free(f, linker.alloc, addr=wire.payload)
    f.Break()
  with f.Else():
    # swap_target(A as Wire, B)
    # => swap(&mut heap[A.payload], B)
    previous = yield HeapPort.swap(f, linker.heap, addr=wire.payload, write=other)
    previous = Port(previous)

    with f.If(previous.tag == Tag.NONE):
      # no node at A.payload's old slot, no work left
      f.d.sync += linker.done.eq(True)
      yield
      f.Break()
    with f.Else():
      # free A.payload and `link(previous, b)`
      # in this cycle we only free A.payload
      f.d.on_done += [
        next_a.eq(previous),
        next_b.eq(other),
      ]
      yield AllocPort.free(f, linker.alloc, addr=wire.payload)

def follow(f: Flow, linker: Linker, p):
  p = f.capture(p)

  with f.While(p.tag == Tag.WIRE):
    q = yield HeapPort.read(f, linker.heap, addr=p.payload)
    q = Port(q)

    with f.If(q.tag == Tag.NONE):
      f.Break()
    with f.Else():
      f.d.on_done += p.eq(q)
      yield AllocPort.free(f, linker.alloc, addr=p.payload)

  return p
