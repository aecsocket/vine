from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amflow import Flow
from ivga.mem.heap import HeapPort
from ivga.mem.page_alloc import PageAllocPort
from ivga.mem.pair_alloc import PairAllocPort
from ivga.runtime.linker import LinkerPort

from ivga import Addr, Port, flatten


class GrafterPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_graft": Out(1),
      "req_addr": Out(Addr),
      "req_port": Out(Port),
      "granted": In(1),
      "done": In(1),
    })

class Grafter(wiring.Component):
  control: In(GrafterPort())
  heap: Out(HeapPort())
  pages: Out(PageAllocPort())
  pairs: Out(PairAllocPort())
  linker: Out(LinkerPort())

  def __init__(self):
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m, grafter = Module(), self
    m.d.sync += grafter.done.eq(False)
    return m

def program(f: Flow, grafter: Grafter):
  f.d.comb += grafter.granted.eq(True)

  with f.If(grafter.req_graft):
    addr = f.capture(grafter.req_addr)
    port = f.capture(grafter.req_port)
