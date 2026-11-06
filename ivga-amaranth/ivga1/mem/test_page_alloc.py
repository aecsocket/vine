from amaranth import *
from amaranth.lib import wiring
from amaranth.sim import Simulator
from ivga0.mem.heap import Heap
from ivga0.mem.page_alloc import PageAlloc

PERIOD = 1e-6

m = Module()
heap = m.submodules.heap = Heap()
dut = m.submodules.alloc = PageAlloc(n_pages=8)
wiring.connect(m, dut.heap, heap.control)

async def testbench_foo(ctx):
  assert ctx.get(dut.alloc_ids[0]) == 0
  assert ctx.get(dut.stat_allocs) == 0
  assert ctx.get(dut.stat_frees) == 0
  assert ctx.get(dut.free_count) == 0

  ctx.set(dut.req_alloc, 1)
  await ctx.tick()
  ctx.set(dut.req_alloc, 0)

  assert ctx.get(dut.done)
  assert not ctx.get(dut.oom)
  assert ctx.get(dut.alloc_ids[0]) == 0
  assert ctx.get(dut.stat_allocs) == 1
  await ctx.tick()

  ctx.set(dut.req_free, 1)
  ctx.set(dut.free_ids[0], 0)
  await ctx.tick()
  ctx.set(dut.req_free, 0)
  await ctx.tick()

  assert ctx.get(dut.done)
  assert ctx.get(dut.stat_frees) == 1
  assert ctx.get(dut.free_count) == 1

sim = Simulator(m)
sim.add_clock(PERIOD)
sim.add_testbench(testbench_foo)
with sim.write_vcd("build/test_page_alloc.vcd"):
  sim.run()
