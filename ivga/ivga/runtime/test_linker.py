from amaranth import *
from amaranth.lib import wiring
from amaranth.sim import Simulator
from ivga.mem.alloc import ADDR_EMPTY, Alloc
from ivga.mem.heap import Heap, HeapArbiter
from ivga.mem.stack import Stack
from ivga.runtime.linker import Linker

from ivga import Port, Port2

PERIOD = 1e-6

m = Module()
heap = m.submodules.heap = Heap()
heap_arb = m.submodules.heap_arb = HeapArbiter(2)
alloc = m.submodules.alloc = Alloc(8)
active = m.submodules.active = Stack(Port2, 8)
dut = m.submodules.linker = Linker()
wiring.connect(m, heap_arb.clients[0], alloc.heap)
wiring.connect(m, heap_arb.clients[1], dut.heap)
wiring.connect(m, heap_arb.real, heap.control)
wiring.connect(m, dut.alloc, alloc.control)
wiring.connect(m, dut.active, active.control)

async def testbench_foo(ctx):
  ctx.set(heap.memory.data[0], Port.invalid())
  ctx.set(heap.memory.data[1], Port.wire(2))
  ctx.set(heap.memory.data[2], Port.eraser())
  ctx.set(alloc.empty, 4) # Reserve two aligned pairs for slots 0..3.
  ctx.set(dut.req_link, True)
  ctx.set(dut.req_a, Port.wire(0))
  ctx.set(dut.req_b, Port.wire(1))
  await ctx.tick()
  ctx.set(dut.req_link, False)

  for _ in range(20):
    if ctx.get(dut.done):
      break
    await ctx.tick()
  else:
    raise AssertionError("link did not complete within 20 cycles")
  assert ctx.get(heap.memory.data[0]) == Port.eraser().as_value().value
  assert ctx.get(alloc.free_count) == 0
  assert ctx.get(alloc.free_head) == ADDR_EMPTY.value
  assert ctx.get(alloc.half_free) == 0b11
  assert ctx.get(alloc.stat_frees) == 2

sim = Simulator(m)
sim.add_clock(PERIOD)
sim.add_testbench(testbench_foo)
with sim.write_vcd("build/test_linker.vcd"):
  sim.run()
