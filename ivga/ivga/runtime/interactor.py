from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
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

IDLE = "idle"
ANNIHILATE = "annihilate"
COPY = "copy"

LINKERS = 4

class Interactor(wiring.Component):
  control: In(InteractorPort())
  heap: Out(HeapPort())
  alloc: Out(AllocPort())
  active: Out(StackPort(Port2))
  fault: Out(FaultPort())

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
    self.linkers = [Linker() for _ in range(LINKERS)]
    self.linker_granted = [Signal(1) for _ in range(LINKERS)]
    self.linker_done = [Signal(1) for _ in range(LINKERS)]

    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += self.done.eq(False)

    heap_arb = m.submodules.heap_arb = HeapArbiter(LINKERS)
    alloc_arb = m.submodules.alloc_arb = AllocArbiter(LINKERS)
    active_arb = m.submodules.active_arb = StackArbiter(LINKERS, Port2)
    wiring.connect(m, heap_arb.real, wiring.flipped(self.heap))
    wiring.connect(m, alloc_arb.real, wiring.flipped(self.alloc))
    wiring.connect(m, active_arb.real, wiring.flipped(self.active))
    for i in range(LINKERS):
      linker = m.submodules[f"linker_{i}"] = self.linkers[i]
      wiring.connect(m, heap_arb.clients[i], linker.heap)
      wiring.connect(m, alloc_arb.clients[i], linker.alloc)
      wiring.connect(m, active_arb.clients[i], linker.active)

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(m, self)
      with m.State(ANNIHILATE):
        annihilate(m, self, self.a, self.b, False)
      with m.State(COPY):
        copy(m, self, self.a, self.b, False)

    with m.If(self.stat_reset):
      m.d.sync += [
        self.stat_annihilate.eq(0),
        self.stat_copy.eq(0),
        self.stat_commute.eq(0),
      ]

    return m

def idle(m: Module, interactor: Interactor):
  m.d.comb += interactor.granted.eq(True)

  with m.If(interactor.req_interact):
    m.d.sync += [
      interactor.a.eq(interactor.req_a),
      interactor.b.eq(interactor.req_b),
    ]
    do_interact(interactor, m, interactor.req_a, interactor.req_b)

def do_interact(interact: Interactor, m: Module, a, b):
  with m.If(
    (a.tag == Tag.COMB)
    & (b.tag == Tag.COMB)
    & (a.label == b.label)
  ):
    # N >-< N where both nodes have the same label;
    # annihilate each other
    m.d.sync += [
      interact.linker_granted[0].eq(False),
      interact.linker_granted[1].eq(False),
      interact.linker_done[0].eq(False),
      interact.linker_done[1].eq(False),
    ]
    annihilate(m, interact, a, b, True)

  with m.Elif(
    ((a.tag == Tag.COMB) & (b.tag == Tag.COMB))
    | ((a.tag == Tag.COMB) & (b.tag == Tag.EXT_FN))
    | ((a.tag == Tag.EXT_FN) & (b.tag == Tag.COMB))
  ):
    # (Comb|ExtFn) >-< (Comb|ExtFn); if two Combs,
    # they must not have the same label; so we commute
    # AllocPort.alloc(m, interact.alloc, 4)
    m.next = IDLE # TODO

  with m.Elif(a.tag == Tag.ERASE):
    # Erase >-< B (B1, B2)
    # becomes
    # (Erase >-< B1, Erase >-< B2)
    m.d.sync += [
      interact.linker_granted[0].eq(False),
      interact.linker_granted[1].eq(False),
      interact.linker_done[0].eq(False),
      interact.linker_done[1].eq(False),
    ]
    copy(m, interact, a, b, True)


def annihilate(m: Module, interactor: Interactor, a, b, first_cycle: bool):
  a1, a2 = Port.aux(m, a)
  b1, b2 = Port.aux(m, b)

  all_done = Const(1)
  for i, (link_a, link_b) in enumerate([(a1, b1), (a2, b2)]):
    with m.If(first_cycle | ~interactor.linker_granted[i]):
      linker = interactor.linkers[i]
      LinkerPort.link(m, linker, link_a, link_b)
      m.d.sync += interactor.linker_granted[i].eq(linker.granted)

    if first_cycle:
      all_done = Const(0)
    else:
      with m.If(interactor.linkers[i].done):
        m.d.sync += interactor.linker_done[i].eq(True)
      was_done = interactor.linker_done[i]
      all_done = all_done & (linker.done | was_done)

  with m.If(all_done):
    # (a1 >-< b1), (a2 >-< b2) linked
    m.d.sync += [
      interactor.done.eq(True),
      interactor.stat_annihilate.eq(interactor.stat_annihilate + 1),
    ]
    m.next = IDLE
  with m.Else():
    m.next = ANNIHILATE


def copy(m: Module, interactor: Interactor, a, b, first_cycle: bool):
  b1, b2 = Port.aux(m, b)

  all_done = Const(1)
  for i, link_b in enumerate([b1, b2]):
    with m.If(first_cycle | ~interactor.linker_granted[i]):
      linker = interactor.linkers[i]
      LinkerPort.link(m, linker, a, link_b)
      m.d.sync += interactor.linker_granted[i].eq(linker.granted)

    if first_cycle:
      all_done = Const(0)
    else:
      with m.If(interactor.linkers[i].done):
        m.d.sync += interactor.linker_done[i].eq(True)
      was_done = interactor.linker_done[i]
      all_done = all_done & (linker.done | was_done)

  with m.If(all_done):
    # (Erase >-< B1), (Erase >-< B2) linked
    m.d.sync += [
      interactor.done.eq(True),
      interactor.stat_copy.eq(interactor.stat_copy + 1),
    ]
    m.next = IDLE
  with m.Else():
    m.next = COPY

# def commute_alloc(interact: Interact, m: Module):
#   AllocPort.alloc(m, interact.alloc, 4)
#   with m.If(interact.alloc.done):
#     with m.If(interact.alloc.oom):
#       # TODO some kind of OOM fault handling?
#       m.d.sync += interact.done.eq(True)
#       m.next = IDLE
#     with m.Else():
#       m.d.sync += interact.alloc_addrs.eq(interact.alloc.alloc_addrs)
#       LinkerPort.link(m, interact.linker, )
#       m.next = COMMUTE_LINK_1
