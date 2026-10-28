from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
from ivga.mem.alloc import AllocPort

from ivga import Addr, Port, Stat, Tag, flatten

from .link import LinkPort


class InteractPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_interact": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "done": In(1),
      # stats
      "stat_annihilate": In(Stat),
      "stat_copy": In(Stat),
      "stat_commute": In(Stat),
      "stat_reset": Out(1),
    })

IDLE = "idle"
ANNIHILATE_1 = "annihilate_1"
ANNIHILATE_2 = "annihilate_2"
COPY_1 = "copy_1"
COPY_2 = "copy_2"
COMMUTE_ALLOC = "commute_alloc"

class Interact(wiring.Component):
  control: In(InteractPort())
  link: Out(LinkPort())
  alloc: Out(AllocPort())

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
    self.alloc_addrs = Signal(ArrayLayout(Addr, 4))
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += self.done.eq(False)

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(self, m)
      with m.State(ANNIHILATE_1):
        annihilate_1(self, m)
      with m.State(ANNIHILATE_2):
        annihilate_2(self, m)
      with m.State(COPY_1):
        copy_1(self, m)
      with m.State(COPY_2):
        copy_2(self, m)

    with m.If(self.stat_reset):
      m.d.sync += [
        self.stat_annihilate.eq(0),
        self.stat_copy.eq(0),
        self.stat_commute.eq(0),
      ]

    return m

def idle(interact: Interact, m: Module):
  with m.If(interact.req_interact):
    m.d.sync += [
      interact.a.eq(interact.req_a),
      interact.b.eq(interact.req_b),
    ]
    do_interact_(interact, m, interact.req_a, interact.req_b)

def do_interact_(interact: Interact, m: Module, a, b):
  with m.If(
    (a.tag == Tag.COMB)
    & (b.tag == Tag.COMB)
    & (a.label == b.label)
  ):
    # N >-< N where both nodes have the same label;
    # annihilate each other
    a1, _ = Port.aux(m, a)
    b1, _ = Port.aux(m, b)
    LinkPort.link(m, interact.link, a1, b1)
    m.next = ANNIHILATE_1

  with m.Elif(
    (a.tag == Tag.COMB & b.tag == Tag.COMB)
    | (a.tag == Tag.COMB & b.tag == Tag.EXT_FN)
    | (a.tag == Tag.EXT_FN & b.tag == Tag.COMB)
  ):
    # (Comb|ExtFn) >-< (Comb|ExtFn); if two Combs,
    # they must not have the same label; so we commute
    AllocPort.alloc(m, interact.alloc, 4)
    m.next = COMMUTE_ALLOC

  with m.Elif(a.tag == Tag.ERASE):
    # Erase >-< B (B1, B2)
    # becomes
    # (Erase >-< B1, Erase >-< B2)
    b1, _ = Port.aux(m, b)
    LinkPort.link(m, interact.link, a, b1)
    m.next = COPY_1


def annihilate_1(interact: Interact, m: Module):
  a1, a2 = Port.aux(m, interact.a)
  b1, b2 = Port.aux(m, interact.b)
  LinkPort.link(m, interact.link, a1, b1)
  with m.If(interact.link.done):
    # a1 >-< b1 linked
    LinkPort.link(m, interact.link, a2, b2)
    m.next = ANNIHILATE_2

def annihilate_2(interact: Interact, m: Module):
  _, a2 = Port.aux(m, interact.a)
  _, b2 = Port.aux(m, interact.b)
  LinkPort.link(m, interact.link, a2, b2)
  with m.If(interact.link.done):
    # a2 >-< b2 linked
    m.d.sync += [
      interact.done.eq(True),
      interact.stat_annihilate.eq(interact.stat_annihilate + 1),
    ]
    m.next = IDLE

def copy_1(interact: Interact, m: Module):
  b1, b2 = Port.aux(m, interact.b)
  LinkPort.link(m, interact.link, interact.a, b1)
  with m.If(interact.link.done):
    # Erase >-< B1 linked
    LinkPort.link(m, interact.link, interact.a, b2)
    m.next = COPY_2

def copy_2(interact: Interact, m: Module):
  _, b2 = Port.aux(m, interact.b)
  LinkPort.link(m, interact.link, interact.a, b2)
  with m.If(interact.link.done):
    # Erase >-< B2 linked
    m.d.sync += [
      interact.done.eq(True),
      interact.stat_copy.eq(interact.stat_copy + 1),
    ]
    m.next = IDLE

def commute_alloc(interact: Interact, m: Module):
  AllocPort.alloc(m, interact.alloc, 4)
  with m.If(interact.alloc.done):
    with m.If(interact.alloc.oom):
      # TODO some kind of OOM fault handling?
      m.d.sync += interact.done.eq(True)
      m.next = IDLE
    with m.Else():
      m.d.sync += interact.alloc_addrs.eq(interact.alloc.alloc_addrs)
      LinkPort.link(m, interact.link, )
      m.next = COMMUTE_LINK_1
