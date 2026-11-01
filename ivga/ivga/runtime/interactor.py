from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amflow import Flow, expand
from ivga.mem.alloc import AllocArbiter, AllocPort
from ivga.mem.heap import HeapArbiter, HeapPort
from ivga.mem.stack import StackArbiter, StackPort
from ivga.runtime.fault import FaultPort

from ivga import Addr, Port, Port2, Stat, Tag, flatten

from .linker import Linker, LinkerPort


class InteractorPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_interact": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "granted": In(1),
      "done": In(1),
      # stats
      "stat_annihilate": In(Stat),
      "stat_copy": In(Stat),
      "stat_commute": In(Stat),
      "stat_reset": Out(1),
    })

LINKERS = 8

class Interactor(wiring.Component):
  control: In(InteractorPort())
  heap: Out(HeapPort())
  alloc: Out(AllocPort())
  active: Out(StackPort(Port2))
  fault: Out(FaultPort())

  def __init__(self):
    self.linkers = [Linker() for _ in range(LINKERS)]
    self.heap_arb = HeapArbiter(LINKERS + 1)
    self.alloc_arb = AllocArbiter(LINKERS + 1)
    self.active_arb = StackArbiter(LINKERS, Port2)
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m, interactor = Module(), self
    m.d.sync += interactor.done.eq(False)

    heap_arb = m.submodules.heap_arb = self.heap_arb
    alloc_arb = m.submodules.alloc_arb = self.alloc_arb
    active_arb = m.submodules.active_arb = self.active_arb
    wiring.connect(m, heap_arb.real, wiring.flipped(self.heap))
    wiring.connect(m, alloc_arb.real, wiring.flipped(self.alloc))
    wiring.connect(m, active_arb.real, wiring.flipped(self.active))

    for i in range(LINKERS):
      linker = m.submodules[f"linker_{i}"] = self.linkers[i]
      wiring.connect(m, heap_arb.clients[i], linker.heap)
      wiring.connect(m, alloc_arb.clients[i], linker.alloc)
      wiring.connect(m, active_arb.clients[i], linker.active)

    # all heap/alloc access is arb'd thru `heap_arb`/`alloc_arb`,
    # but some of our own interactor code (eg in commutation)
    # needs to access the heap or allocator.
    # so we need an extra client - ourselves.
    own_heap = self.own_heap = HeapPort().create()
    wiring.connect(m, heap_arb.clients[LINKERS], own_heap)
    own_alloc = self.own_alloc = AllocPort().create()
    wiring.connect(m, alloc_arb.clients[LINKERS], own_alloc)

    m.d.sync += interactor.done.eq(False)
    expand(m, lambda f: program(f, interactor))

    with m.If(interactor.stat_reset):
      m.d.sync += [
        interactor.stat_annihilate.eq(0),
        interactor.stat_copy.eq(0),
        interactor.stat_commute.eq(0),
      ]

    return m

def program(f: Flow, interactor: Interactor):
  f.d.comb += interactor.granted.eq(True)

  with f.If(interactor.req_interact):
    a = f.capture(interactor.req_a)
    b = f.capture(interactor.req_b)

    with f.If(
      (a.tag == Tag.COMB)
      & (b.tag == Tag.COMB)
      & (a.label == b.label)
    ):
      # N >-< N where both nodes have the same label;
      # annihilate each other
      yield from annihilate(f, interactor, a, b)

    with f.Elif(
      ((a.tag == Tag.COMB) & (b.tag == Tag.COMB))
      | ((a.tag == Tag.COMB) & (b.tag == Tag.EXT_FN))
      | ((a.tag == Tag.EXT_FN) & (b.tag == Tag.COMB))
    ):
      # (Comb|ExtFn) >-< (Comb|ExtFn); if two Combs,
      # they must not have the same label, since we handled
      # that in the last case, so we commute
      yield from commute(f, interactor, a, b)

    with f.Elif(a.tag == Tag.ERASE):
      # Erase >-< B (B1, B2)
      # => (Erase >-< B1, Erase >-< B2)
      yield from copy(f, interactor, a, b)

def annihilate(f: Flow, interactor: Interactor, a, b):
  a1, a2 = Port.aux(a)
  b1, b2 = Port.aux(b)
  f.d.on_done += [
    interactor.done.eq(True),
    interactor.stat_annihilate.eq(interactor.stat_annihilate + 1),
  ]
  yield LinkerPort.link_batch(f, [
    (interactor.linkers[0], a1, b1),
    (interactor.linkers[1], a2, b2),
  ])

def commute(f: Flow, interactor: Interactor, a, b):
  bases, oom = yield AllocPort.alloc(f, interactor.alloc, n_pairs=4)
  with f.If(oom):
    # TODO raise a fault
    f.d.sync += interactor.done.eq(True)
    yield
  with f.Else():
    def new_port(base):
      return (
        Port.new(a.tag, a.label, base),
        Port.new(Const(Tag.WIRE), Const(0, 16), base),
        Port.new(Const(Tag.WIRE), Const(0, 16), Addr.other_half(base)),
      )

    # port, left, right
    a1p, a1l, a1r = new_port(bases[0])
    a2p, a2l, a2r = new_port(bases[1])
    b1p, b1l, b1r = new_port(bases[2])
    b2p, b2l, b2r = new_port(bases[3])

    # left/right halves of ports A/B
    al, ar = Port.aux(a)
    bl, br = Port.aux(b)

    yield LinkerPort.link_batch(f, [
      # link fresh wires
      (interactor.linkers[0], a1l, b1l),
      (interactor.linkers[1], a1r, b2l),
      (interactor.linkers[2], a2l, b1r),
      (interactor.linkers[3], a2r, b2r),

      # link node wires and freshly made ports
      (interactor.linkers[4], al, b1p),
      (interactor.linkers[5], ar, b2p),
      (interactor.linkers[6], bl, a1p),
      (interactor.linkers[7], br, a2p),
    ])

def copy(f: Flow, interactor: Interactor, a, b):
  x, y = Port.aux(b)
  f.d.on_done += [
    interactor.done.eq(True),
    interactor.stat_copy.eq(interactor.stat_copy + 1),
  ]
  yield LinkerPort.link_batch(f, [
    (interactor.linkers[0], x, a),
    (interactor.linkers[1], y, a),
  ])
