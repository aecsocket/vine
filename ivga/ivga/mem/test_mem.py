from amaranth import *
from amaranth.lib import wiring
from amaranth.sim import Simulator
from ivga.mem.alloc import Alloc
from ivga.mem.heap import Heap

PERIOD = 1e-6

m = Module()
heap = m.submodules.heap = Heap()
dut = m.submodules.alloc = Alloc(8)
wiring.connect(m, dut.heap, heap.control)

async def testbench_foo(ctx):
  assert ctx.get(dut.done) == False
  ctx.set(dut.req_alloc, 2)
  await ctx.tick()
  ctx.set(dut.req_alloc, 0)
  assert ctx.get(dut.done) == True
  assert ctx.get(dut.alloc_addrs[0]) == 0
  assert ctx.get(dut.alloc_addrs[1]) == 1

  ctx.set(dut.req_free, 2)
  ctx.set(dut.free_addrs, [0, 1])
  await ctx.tick()
  ctx.set(dut.req_free, 0)
  await ctx.tick().repeat(2)
  assert ctx.get(dut.done) == True

sim = Simulator(m)
sim.add_clock(PERIOD)
sim.add_testbench(testbench_foo)
with sim.write_vcd("build/test_mem.vcd"):
  sim.run()
