from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory, ReadPort, WritePort
from amaranth.lib.wiring import In, Out
from amflow import Flow, ValueLike
from ivga.util.arbiter import make_arbiter_type

from ivga import flatten


class StackPort(wiring.Signature):
  """
  # Operations

  - `req_push()`: push `push_data` onto the top of the stack.
    Must only be raised if `~full`.

  - `req_pop()`: pop the last value off the stack and place it into `pop_data`.
    Must only be raised if `~empty`.

  The shape of data on the stack is not required to be a word, since the stack
  doesn't use `Heap`.
  """

  def __init__(self, shape):
    super().__init__({
      "req_push": Out(1),
      "push_data": Out(shape),
      "req_pop": Out(1),
      "pop_data": In(shape),

      "granted": In(1),
      "done": In(1),
      "empty": In(1),
      "full": In(1),
    })

  @staticmethod
  def push(f: Flow, stack, *, data: ValueLike) -> None:
    can_push = ~stack.full
    f.d.comb += [
      stack.req_push.eq(can_push),
      stack.push_data.eq(data),
    ]
    f.d.until += can_push & stack.granted

  @staticmethod
  def pop(f: Flow, stack) -> ValueLike:
    result = f.capture(stack.pop_data, after_step=True)
    can_pop = ~stack.empty
    f.d.comb += stack.req_pop.eq(can_pop)
    f.d.until += can_pop & stack.granted
    return result


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
    memory = m.submodules.memory = Memory(
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

    # push; don't always enable tho
    m.d.comb += [
      mem_write.addr.eq(self.top),
      mem_write.data.eq(self.push_data),
    ]

    # pop; don't always enable tho
    m.d.comb += self.pop_data.eq(mem_read.data)

    with m.If(self.req_push):
      push(self, m, mem_read, mem_write)
    with m.Elif(self.req_pop):
      pop(self, m, mem_read, mem_write)

    has_request = self.req_push | self.req_pop
    m.d.comb += self.granted.eq(has_request)

    return m

def push(stack, m: Module, mem_read: ReadPort, mem_write: WritePort):
  with m.If(stack.full):
    m.d.sync += stack.done.eq(True)
  with m.Else():
    m.d.comb += mem_write.en.eq(True)
    m.d.sync += [
      stack.top.eq(stack.top + 1),
      stack.done.eq(True),
    ]

def pop(stack, m: Module, mem_read: ReadPort, mem_write: WritePort):
  with m.If(stack.empty):
    m.d.sync += stack.done.eq(True)
  with m.Else():
    m.d.comb += [
      mem_read.en.eq(True),
      mem_read.addr.eq(stack.top - 1),
    ]
    m.d.sync += [
      stack.top.eq(stack.top - 1),
      stack.done.eq(True),
    ]

StackArbiter = make_arbiter_type(
  StackPort,
  lambda client: client.req_push | client.req_pop,
  always_out=("empty", "full"),
)
