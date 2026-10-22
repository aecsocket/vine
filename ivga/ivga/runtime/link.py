from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga.ty import Port, Tag

IDLE = "idle"
DO_LINK = "do_link"
FOLLOW = "follow"

class Link(wiring.Component):
  req_link: In(1)
  req_a: In(Port)
  req_b: In(Port)

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
    super().__init__()

  def elaborate(self, platform):
    m = Module()

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(self, m)
      with m.State(DO_LINK):
        do_link(self, m)

    return m

def idle(link: Link, m: Module):
  with m.If(link.req_link):
    m.d.sync += [
      link.a.eq(link.req_a),
      link.b.eq(link.req_b),
    ]
    m.next = DO_LINK

def do_link(link: Link, m: Module):
  with m.If(link.a.tag == Tag.WIRE):
    # follow A to the end, and link to B
    m.next = FOLLOW
    pass
