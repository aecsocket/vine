from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike, expand
from ivga.mem.alloc import AllocPort
from ivga.mem.heap import HeapPort
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


class Linker(wiring.Component):
  control: In(LinkerPort())
  heap: Out(HeapPort())
  alloc: Out(AllocPort())
  active: Out(StackPort(Port2))
  fault: Out(FaultPort())

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
    self.previous = Signal(Port)
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m, linker = Module(), self
    m.d.sync += self.done.eq(False)
    expand(m, lambda f: program(f, linker))

    with m.If(linker.stat_reset):
      m.d.sync += linker.stat_erase.eq(0)

    return m

def program(f: Flow, linker: Linker):
  with f.If(linker.req_link):
    a = f.capture(linker.req_a)
    b = f.capture(linker.req_b)
    continues = f.capture(1)

    with f.While(continues):
      yield from do_link(f, linker, a, b, continues)

def do_link(f: Flow, linker: Linker, a, b, continues):
  with f.If(a.tag == Tag.WIRE):
    with f.If(b.tag == Tag.WIRE):
      # A and B are wires; follow B to the end
      # then link A and the tail of B
      yield from follow(f, linker, a, b, continues)
    with f.Else():
      # A is a wire, but B isn't, so we can go straight to connecting
      yield from connect(f, linker, a, b, continues)

  with f.Elif(b.tag == Tag.WIRE):
    # A must not be a wire; go straight to connecting
    yield from connect(f, linker, a, b, continues)

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
      continues.eq(False),
    ]
    yield

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
      continues.eq(False),
    ]
    yield

  with f.Else():
    # all other work goes on the worklist
    # TODO: active_fast/active_slow like in IVM
    with f.If(linker.active.full):
      # fault if active worklist is full
      f.d.comb += linker.fault.active_full.eq(True)
      f.d.sync += linker.done.eq(True)
      yield
    with f.Else():
      active_pair = Signal(Port2)
      f.d.comb += [
        active_pair.a.eq(a),
        active_pair.b.eq(b),
      ]
      yield StackPort.push(f, linker.active, data=active_pair)

def follow(f: Flow, linker: Linker, p) -> ValueLike:
  p = f.capture(p)

  with f.While(p.tag == Tag.WIRE):
    q = Port(yield HeapPort.read(f, addr=p.payload))

    with f.If(q.tag != Tag.INVALID):
      f.d.on_done += p.eq(q)
      yield AllocPort.free_one(f, linker.alloc, addr=p.payload)

  return p

def connect(f: Flow, linker: Linker, a, b, continues):
  with f.If(a == b):
    # A's bits compose a wire, and B has the same bits,
    # so B is the same wire as A.
    # so we just free `A.payload`, and don't do any real connecting.
    yield AllocPort.free_one(f, addr=a.payload)
  with f.Else():
    # swap_target(A as Wire, B)
    # => swap(&mut heap[A.payload], B)
    previous = yield HeapPort.swap(f, addr=a.payload, write=b)
    with f.If(previous.tag == Tag.INVALID):
      # no node at A.payload's old slot, no work left
      f.d.sync += linker.done.eq(True)
      yield
    with f.Else():
      # free A.payload and `link(previous, b)`
      # in this cycle we only free A.payload
      yield AllocPort.free_one(f, addr=a.payload)
      # TODO link(previous, b)
