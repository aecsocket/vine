from amaranth import *
from amaranth.lib import wiring
from amaranth.sim import Simulator
from ivga0.mem.heap import Heap, HeapArbiter
from ivga0.mem.pair_alloc import PairAlloc
from ivga0.mem.stack import Stack
from ivga0.runtime.interact import Interact

from ivga0 import Port, Port2

PERIOD = 1e-6

m = Module()
heap = m.submodules.heap = Heap()
heap_arb = m.submodules.heap_arb = HeapArbiter(2)
alloc = m.submodules.alloc = PairAlloc(8)
active = m.submodules.active = Stack(Port2, 8)
dut = m.submodules.interact = Interact()
wiring.connect(m, heap_arb.clients[0], alloc.heap)
wiring.connect(m, heap_arb.clients[1], dut.heap)
wiring.connect(m, heap_arb.real, heap.control)
wiring.connect(m, dut.pairs, alloc.control)
wiring.connect(m, dut.active, active.control)

async def testbench_foo(ctx):
  ctx.set(heap.memory.data[0], Port.none())
  ctx.set(heap.memory.data[1], Port.none())
  ctx.set(heap.memory.data[2], Port.eraser())
  ctx.set(heap.memory.data[3], Port.none())
  ctx.set(alloc.empty, Const(4)) # since we've alloc'd 4 slots
  ctx.set(dut.req_interact, True)
  ctx.set(dut.req_a, Port.comb(0, 7))
  ctx.set(dut.req_b, Port.comb(2, 7))
  await ctx.tick()
  ctx.set(dut.req_interact, False)

  await ctx.tick().repeat(20)
  # assert ctx.get(dut.done) == True
  # assert ctx.get(heap.memory.data[0]) == Port.eraser().as_value().value

sim = Simulator(m)
sim.add_clock(PERIOD)
sim.add_testbench(testbench_foo)
with sim.write_vcd("build/test_interactor.vcd"):
  sim.run()
