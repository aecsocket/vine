from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga import Port, Tag, flatten

from .link import LinkPort


class InteractPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_interact": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "done": In(1),
    })

IDLE = "idle"
ANNIHILATE_1 = "annihilate_1"
ANNIHILATE_2 = "annihilate_2"

class Interact(wiring.Component):
  control: In(InteractPort())
  link: Out(LinkPort())

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
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
    m.d.sync += interact.done.eq(True)
    m.next = IDLE
