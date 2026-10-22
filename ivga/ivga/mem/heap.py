from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga import Addr, Word


class HeapPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "read": Out(1),
      "write": Out(1),
      "addr": Out(Addr),
      "write_data": Out(Word),
      "done": In(1),
      "read_data": In(Word),
    })

  @staticmethod
  def read(m: Module, heap, addr):
    m.d.comb += [
      heap.read.eq(True),
      heap.addr.eq(addr),
    ]

  @staticmethod
  def write(m: Module, heap, addr, data):
    m.d.comb += [
      heap.write.eq(True),
      heap.addr.eq(addr),
      heap.write_data.eq(data),
    ]
