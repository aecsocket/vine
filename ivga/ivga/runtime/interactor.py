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
COMMUTE_ALLOC_REQ = "commute_alloc_req"
COMMUTE_ALLOC_WAIT = "commute_alloc_wait"

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
    self.commute_allocs = [Signal(Port) for _ in range(8)]
    self.commute_alloc_idx = Signal(range(8))

    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += self.done.eq(False)

    heap_arb = m.submodules.heap_arb = HeapArbiter(LINKERS + 1)
    alloc_arb = m.submodules.alloc_arb = AllocArbiter(LINKERS + 1)
    active_arb = m.submodules.active_arb = StackArbiter(LINKERS, Port2)
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

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(m, self)
      with m.State(ANNIHILATE):
        annihilate(m, self, self.a, self.b, False)
      with m.State(COPY):
        copy(m, self, self.a, self.b, False)
      with m.State(COMMUTE_ALLOC_REQ):
        commute_alloc_req(m, self)
      with m.State(COMMUTE_ALLOC_WAIT):
        commute_alloc_wait(m, self)

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
    m.d.sync += [
      self.commute_allocs[i].eq()
      for i in range(8)
    ]
    commute_alloc_req(m, interact)

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

def commute_alloc_req(m: Module, interactor: Interactor):
  AllocPort.alloc(m, interactor.own_alloc, 8)
  with m.If(interactor.alloc.granted):
    m.next = COMMUTE_ALLOC_WAIT
  with m.Else():
    m.next = COMMUTE_ALLOC_REQ

def commute_alloc_wait(m: Module, interactor: Interactor):
  with m.If(interactor.own_alloc.oom):
    # TODO some kind of OOM fault handling?
    m.d.sync += interactor.done.eq(True)
    m.next = IDLE
  with m.Elif(interactor.own_alloc.done):
    # nodes allocated; wire the first 4 together
    a_1 = Signal(Port)
    m.d.comb += a_1.eq({
      "tag": interactor.a.tag,
      "payload": interactor.own_alloc.alloc_addrs[0],
      "label": interactor.a.label,
    })
    a_2 = Signal(Port)
    m.d.comb += a_2.eq({
      "tag": interactor.a.tag,
      "payload": interactor.own_alloc.alloc_addrs[1],
      "label": interactor.a.label,
    })

    b_1 = Signal(Port)
    m.d.comb += b_1.eq({
      "tag": interactor.b.tag,
      "payload": interactor.own_alloc.alloc_addrs[2],
      "label": interactor.b.label,
    })
    b_2 = Signal(Port)
    m.d.comb += b_2.eq({
      "tag": interactor.b.tag,
      "payload": interactor.own_alloc.alloc_addrs[3],
      "label": interactor.b.label,
    })

def commute_link_fresh_req(m: Module, interactor: Interactor):
  HeapPort.write(m, interactor.own_heap, interactor.commute_allocs[interactor.commute_alloc_idx], Port.invalid())
  with m.If(interactor.own_heap.granted):
    m.next = COMMUTE_LINK_FRESH_WAIT
  with m.Else():
    m.next = COMMUTE_LINK_FRESH_REQ

def commute_link_fresh_wait(m: Module, interactor: Interactor):
  with m.If(interactor.own_heap.done):
    pass
