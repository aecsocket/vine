from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from ivga.mem.heap import HeapPort
from ivga.util.arbiter import Arbiter


class HeapArbiter(wiring.Component):
  def __init__(self, width: int):
    self.width = width
    super().__init__({
      "clients": In(HeapPort()).array(width),
      "real": Out(HeapPort()),
    })

  def elaborate(self, platform):
    m = Module()

    arbiter = m.submodules.arbiter = Arbiter(self.width)

    for i, client in enumerate(self.clients):
      m.d.comb += arbiter.request[i].eq(client.read | client.write)
      with m.If(arbiter.grant[i]):
        m.d.comb += [
          self.real.read.eq(client.read),
          self.real.write.eq(client.write),
          self.real.addr.eq(client.addr),
          self.real.write_data.eq(client.write_data),
          client.done.eq(self.real.done),
          client.read_data.eq(self.real.read_data),
        ]

    return m
#
# def make_shared_heap(m: Module, width: int) -> tuple[Heap, HeapArbiter]:
#   heap = m.submodules.heap = Heap()
#   arb = m.submodules.heap_arb = HeapArbiter(width)
#   wiring.connect(m, arb.real, heap.control)
#   return heap, arb
