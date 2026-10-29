from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amaranth.sim import Simulator

from amflow import ReadMemory, WriteMemory, elaborate_flow


class Dummy(wiring.Component):
  smth_external: In(1)
  go_out: Out(1)

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
    elaborate_flow(m, do_something(self))

    return m

def do_something(dummy: Dummy):
  yield WriteMemory("init_6", dummy.memory_write, addr=6, data=0x42)
  yield WriteMemory("init_7", dummy.memory_write, addr=6, data=0x99)

  a = yield ReadMemory("read_6", dummy.memory_read, addr=6)
  b = yield ReadMemory("read_7", dummy.memory_read, addr=7)
  c = a + b
  yield WriteMemory("write_8", dummy.memory_write, addr=8, data=c)

async def dummy_testbench(ctx):
  await ctx.tick().repeat(10)


dut = Dummy()
sim = Simulator(dut)
sim.add_clock(1e-6)
sim.add_testbench(dummy_testbench)
with sim.write_vcd("build/test_amflow.vcd"):
  sim.run()
