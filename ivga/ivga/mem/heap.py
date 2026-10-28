from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out

from ivga import Addr, Word


class HeapPort(wiring.Signature):
  """
  # Operations

  - `read()`: read the word at `addr`, placing the result in `read_data`.

  - `write()`: write the word `write_data` to address `addr`.

  If both `read()` and `write()` are high on a cycle, then the value at `addr`
  will be swapped with the value in `write_data`, and the old value will be
  available at `read_data`. In Rust terms:

  ```rs
  let read_data = swap(&mut addr, write_data);
  ```
  """

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

def make_heap():
  return Memory(
    shape=Word,
    depth=1024,
    init=[]
  )
