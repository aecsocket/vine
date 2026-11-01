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
  ctx.set(dut.req_alloc, 2)
  assert ctx.get(dut.alloc_bases[0]) == 0
  assert ctx.get(dut.alloc_bases[1]) == 0
  assert ctx.get(dut.stat_allocs) == 0
  assert ctx.get(dut.stat_frees) == 0
  assert ctx.get(dut.free_count) == 0

  ctx.set(dut.req_alloc, 2)
  await ctx.tick()
  ctx.set(dut.req_alloc, 0)

  assert not ctx.get(dut.oom)
  assert ctx.get(dut.alloc_bases[0]) == 0
  assert ctx.get(dut.alloc_bases[1]) == 2
  assert ctx.get(dut.stat_allocs) == 4

  ctx.set(dut.req_free, 1)
  ctx.set(dut.free_addrs[0], 1)
  await ctx.tick()
  ctx.set(dut.req_free, 0)
  await ctx.tick()

  assert ctx.get(dut.alloc_bases[0]) == 0
  assert ctx.get(dut.stat_frees) == 1
  assert ctx.get(dut.free_count) == 0
  await ctx.tick()

  ctx.set(dut.req_free, 1)
  ctx.set(dut.free_addrs[0], 0)
  await ctx.tick()
  ctx.set(dut.req_free, 0)
  await ctx.tick().repeat(4)

  assert ctx.get(dut.stat_frees) == 2
  assert ctx.get(dut.free_count) == 2

sim = Simulator(m)
sim.add_clock(PERIOD)
sim.add_testbench(testbench_foo)
with sim.write_vcd("build/test_mem.vcd"):
  sim.run()
