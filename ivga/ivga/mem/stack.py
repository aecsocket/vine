from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory, ReadPort, WritePort
from amaranth.lib.wiring import In, Out

from ivga import flatten


class StackPort(wiring.Signature):
  def __init__(self, shape):
    super().__init__({
      "req_push": Out(1),
      "push_data": Out(shape),

      "req_pop": Out(1),
      "pop_data": In(shape),

      "done": In(1),
      "empty": In(1),
      "full": In(1),
    })

  @staticmethod
  def push(m: Module, stack, data):
    m.d.comb += [
      stack.req_push.eq(True),
      stack.push_data.eq(data),
    ]

  @staticmethod
  def pop(m: Module, stack):
    m.d.comb += stack.req_pop.eq(True)

class Stack(wiring.Component):
  def __init__(self, shape, depth: int):
    assert depth > 0

    self.shape = shape
    self.depth = depth

    self.top = Signal(range(depth + 1))
    super().__init__({
      "control": In(StackPort(shape))
    })
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()

    m.submodules.memory = memory = Memory(
      shape=self.shape,
      depth=self.depth,
      init=[]
    )
    mem_read = memory.read_port()
    mem_write = memory.write_port()
    m.d.sync += self.done.eq(False)

    m.d.comb += [
      self.empty.eq(self.top == 0),
      self.full.eq(self.top == self.depth),
      mem_read.en.eq(False),
      mem_write.en.eq(False),
    ]

    with m.If(self.req_push):
      push(self, m, mem_read, mem_write)
    with m.Elif(self.req_pop):
      pop(self, m, mem_read, mem_write)

    return m

def push(stack: Stack, m: Module, mem_read: ReadPort, mem_write: WritePort):
  with m.If(stack.full):
    m.d.sync += stack.done.eq(True)
  with m.Else():
    m.d.comb += [
      mem_write.en.eq(True),
      mem_write.addr.eq(stack.top),
      mem_write.data.eq(stack.push_data),
    ]
    m.d.sync += [
      stack.top.eq(stack.top + 1),
      stack.done.eq(True),
    ]

def pop(stack: Stack, m: Module, mem_read: ReadPort, mem_write: WritePort):
  with m.If(stack.empty):
    m.d.sync += stack.done.eq(True)
  with m.Else():
    m.d.comb += [
      mem_read.en.eq(True),
      mem_read.addr.eq(stack.top - 1),
      stack.pop_data.eq(mem_read.data),
    ]
    m.d.sync += [
      stack.top.eq(stack.top - 1),
      stack.done.eq(True),
    ]
