from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
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

LINKERS = 4

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

    heap_arb = self.heap_arb = m.submodules.heap_arb
    alloc_arb = self.alloc_arb = m.submodules.alloc_arb
    active_arb = self.active_arb = m.submodules.active_arb
    wiring.connect(m, heap_arb.real, wiring.flipped(self.heap))
    wiring.connect(m, alloc_arb.real, wiring.flipped(self.alloc))
    wiring.connect(m, active_arb.real, wiring.flipped(self.active))

    for i in range(LINKERS):
      linker = self.linkers[i] = m.submodules[f"linker_{i}"]
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
  a1, a2 = Port.aux(f, a)
  b1, b2 = Port.aux(f, b)
  yield LinkerPort.link_batch(f, [
    (interactor.linkers[0], a1, b1),
    (interactor.linkers[1], a2, b2),
  ])
  f.d.sync += [
    interactor.done.eq(True),
    interactor.stat_annihilate.eq(interactor.stat_annihilate + 1),
  ]
  yield

def commute(f: Flow, interactor: Interactor, a, b):
  addr_bases, oom = yield AllocPort.alloc(f, n_pairs=4)
  with f.If(oom):
    # TODO raise a fault
    f.d.sync += interactor.done.eq(True)
  with f.Else():
    a1, a2, b1, b2 = addr_bases[0], addr_bases[1], addr_bases[2], addr_bases[3]
    a1_1, a1_2 = a1, Addr.other_half(a1)
    a2_1, a2_2 = a2, Addr.other_half(a2)
    b1_1, b1_2 = b1, Addr.other_half(b1)
    b2_1, b2_2 = b2, Addr.other_half(b2)

    a01, a02 = Port.aux(f, a)
    b01, b02 = Port.aux(f, b)

    yield LinkerPort.link_batch(f, [
      (interactor.linkers[0], a1)
    ])

def copy(f: Flow, interactor: Interactor, a, b):
  x, y = Port.aux(f, b)
  yield LinkerPort.link_batch(f, [
    (interactor.linkers[0], x, a),
    (interactor.linkers[1], y, a),
  ])
  f.d.sync += [
    interactor.done.eq(True),
    interactor.stat_copy.eq(interactor.stat_copy + 1),
  ]
  yield
