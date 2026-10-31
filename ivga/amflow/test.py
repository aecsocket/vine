from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amaranth.sim import Simulator

from amflow import Flow, expand, step, sync, tick
from amflow.memory import memory_read, memory_write


class Dummy(wiring.Component):
  smth_external: In(1)
  started: Out(1)
  done: Out(1)

  def __init__(self):
    self.memory = Memory(
      shape=unsigned(8),
      depth=16,
      init=[],
    )
    self.memory_read = self.memory.read_port()
    self.memory_write = self.memory.write_port()
    super().__init__()

  def elaborate(self, platform):
    m = Module()
    m.submodules.memory = self.memory

    with m.If(~self.done):
      expand(m, lambda f: do_something(f, self))

    return m


def do_something(f: Flow, dummy: Dummy):
  f.m.d.comb += dummy.memory_read.en.eq(False)

  yield memory_write(f, dummy.memory_write, addr=6, data=0x42)
  yield memory_write(f, dummy.memory_write, addr=7, data=0x99)

  _ = sync(f, dummy.started.eq(True))
  a = yield memory_read(f, dummy.memory_read, addr=6)
  b = yield memory_read(f, dummy.memory_read, addr=7)
  with f.If(a == b):
    yield memory_write(f, dummy.memory_write, addr=8, data=a + b)
  with f.Else():
    yield memory_write(f, dummy.memory_write, addr=8, data=b)

  counter = Signal(range(5))
  yield sync(f, counter.eq(4))
  with f.While(counter != 0):
    x = yield memory_read(f, dummy.memory_read, addr=2)
    _ = step(f, on_done=counter.eq(counter - 11))
    yield memory_write(f, dummy.memory_write, addr=2, data=x + 1)

  _ = sync(f, dummy.done.eq(True))
  yield tick(f)


async def dummy_testbench(ctx):
  await ctx.tick().repeat(30)


dut = Dummy()
sim = Simulator(dut)
sim.add_clock(1e-6)
sim.add_testbench(dummy_testbench)
with sim.write_vcd("build/test_amflow.vcd"):
  sim.run()
