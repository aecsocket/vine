from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga import ERASER, Port
from ivga.mem.alloc import Alloc
from ivga.mem.heap import make_heap
from ivga.runtime.interact import Interact
from ivga.runtime.link import Link


class Arbiter(wiring.Component):
  request_a: In(1)
  request_b: In(1)
  grant_a: Out(1)
  grant_b: Out(1)

  def __init__(self):
    self.current = Signal(range(3))
    super().__init__()

  def elaborate(self, platform):
    m = Module()

    m.d.comb += [
      self.grant_a.eq(self.current == 0),
      self.grant_b.eq(self.current == 1),
    ]

    with m.If(self.current == 2):
      m.d.sync += self.current.eq(0)
    with m.Else():
      m.d.sync += self.current.eq(self.current + 1)

    return m

class Processor(wiring.Component):
  def __init__(self):
    super().__init__({})

  def elaborate(self, platform):
    m = Module()

    m.submodules.heap = heap = make_heap()
    heap_read = heap.read_port()
    heap_write = heap.write_port()
    m.submodules.heap_arb = heap_arb = Arbiter()
    m.submodules.alloc = alloc = Alloc(capacity=512)
    m.submodules.link = link = Link()
    m.submodules.interact = interact = Interact()

    with m.If(heap_arb.grant_a):
      # alloc gets access to the heap
      m.d.comb += [
        heap_read.en.eq(alloc.heap.read),
        heap_read.addr.eq(alloc.heap.addr),
        alloc.heap.read_data.eq(heap_read.data),

        heap_write.en.eq(alloc.heap.write),
        heap_write.addr.eq(alloc.heap.addr),
        heap_write.data.eq(alloc.heap.write_data),
      ]
    with m.If(heap_arb.grant_b):
      # link gets access to the heap
      m.d.comb += [
        heap_read.en.eq(link.heap.read),
        heap_read.addr.eq(link.heap.addr),
        link.heap.read_data.eq(heap_read.data),

        heap_write.en.eq(link.heap.write),
        heap_write.addr.eq(link.heap.addr),
        heap_write.data.eq(link.heap.write_data),
      ]

    a = Const(ERASER, Port)
    b = Const(ERASER, Port)

    m.d.comb += [
      interact.req_interact.eq(True),
      interact.req_a.eq(a),
      interact.req_b.eq(b),
    ]

    return m
