from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.data import ArrayLayout
from amaranth.lib.wiring import In, Out
from ivga.mem.alloc import AllocPort
from ivga.mem.heap import HeapPort
from ivga.mem.stack import StackPort
from ivga.runtime.fault import FaultPort
from ivga.runtime.linker import Linker

from ivga import Port, Port2, Stat, flatten


class LinkerBatchPort(wiring.Signature):
  def __init__(self, width: int):
    super().__init__({
      "req_link": Out(range(width + 1)),
      "req_a": Out(ArrayLayout(Port, width)),
      "req_b": Out(ArrayLayout(Port, width)),
      "done": In(1),
      # stats
      "stat_erase": In(Stat),
      "stat_reset": Out(1),
    })

  @staticmethod
  def link(m: Module, batch, count, a, b):
    m.d.comb += [
      batch.req_link.eq(count),
      batch.req_a.eq(a),
      batch.req_b.eq(b),
    ]

IDLE = "idle"
WAIT = "wait"

class LinkerBatch(wiring.Component):
  def __init__(self, width: int):
    assert width > 0

    super().__init__({
      "control": In(LinkerBatchPort(width)),
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
      m.submodules[f"linker_{i}"] = linker = Linker()
      linkers.append(linker)

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(self, m)
      with m.State(WAIT):
        wait(self, m)

    for i, linker in enumerate(linkers):
      m.d.comb += [
        linker.req_link.eq(self.req_link[i]),
        linker.req_a.eq(self.req_a[i]),
        linker.req_b.eq(self.req_b[i]),
      ]

    return m
