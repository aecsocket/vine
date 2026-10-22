from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from ivga.ty import Addr, Word


class HeapPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "read_enable": Out(1),
      "read_addr": Out(Addr),
      "read_data": In(Word),
      "write_enable": Out(1),
      "write_addr": Out(Addr),
      "write_data": Out(Word),
    })
