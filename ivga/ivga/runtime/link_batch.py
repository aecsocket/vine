from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
from ivga.mem.alloc import AllocPort
from ivga.mem.heap import HeapPort
from ivga.mem.stack import StackPort
from ivga.runtime.fault import FaultPort
from ivga.runtime.link import Link

from ivga import Port, Port2, Stat, flatten


class LinkBatchPort(wiring.Signature):
  def __init__(self, batch: int):
    super().__init__({
      "req_link": Out(range(batch + 1)),
      "req_a": Out(ArrayLayout(Port, batch)),
      "req_b": Out(ArrayLayout(Port, batch)),
      "done": In(1),
      # stats
      "stat_erase": In(Stat),
      "stat_reset": Out(1),
    })

  @staticmethod
  def link(m: Module, link_batch, count, a, b):
    m.d.comb += [
      link_batch.req_link.eq(count),
      link_batch.req_a.eq(a),
      link_batch.req_b.eq(b),
    ]

class LinkBatch(wiring.Component):
  def __init__(self, batch: int):
    assert batch > 0

    super().__init__({
      "control": In(LinkBatchPort(batch)),
      "heap": Out(HeapPort()),
      "alloc": Out(AllocPort()),
      "active": Out(StackPort(Port2)),
      "fault": Out(FaultPort()),
    })
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += self.done.eq(False)

    linkers = []
    for i in range(self.batch):
      m.submodules[f"linker_{i}"] = linker = Link()
      linkers.append(linker)

    for i, linker in enumerate(linkers):
      m.d.comb += [
        linker.req_link.eq(self.req_link[i]),
        linker.req_a.eq(self.req_a[i]),
        linker.req_b.eq(self.req_b[i]),
      ]

    return m
