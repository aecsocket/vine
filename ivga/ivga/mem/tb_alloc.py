from amaranth import *
from amaranth.lib.memory import Memory
from amaranth.sim import Simulator, SimulatorContext
from ivga.mem.alloc import Alloc
from ivga.ty import ADDR_WIDTH, SYNC, Word

PERIOD = 0.00_000_001 # 1ns
CAPACITY = 4

def setup():
  dut = Alloc(capacity=CAPACITY)
  m = Module()
  m.submodules.alloc = dut

  memory = Memory(
    shape=Word,
    depth=1 << ADDR_WIDTH,
    init=[]
  )
  read = memory.read_port(domain=SYNC)
  write = memory.write_port(domain=SYNC)
  m.submodules.memory = memory

  m.d.comb += [
    read.en.eq(dut.heap.read),
    read.addr.eq(dut.heap.addr),
    dut.heap.read_data.eq(read.data),

    write.en.eq(dut.heap.write),
    write.addr.eq(dut.heap.addr),
    write.data.eq(dut.heap.write_data),

    dut.heap.done.eq(True),
  ]

  sim = Simulator(m)
  sim.add_clock(PERIOD)
  return dut, sim

def assert_allocated_at_(ctx: SimulatorContext, dut: Alloc, addr: int):
  assert ctx.get(dut.done) == True
  assert ctx.get(dut.oom) == False
  assert ctx.get(dut.alloc_addr) == addr

def test_bump_alloc():
  dut, sim = setup()

  async def bench(ctx: SimulatorContext):
    assert_allocated_at = lambda addr: assert_allocated_at_(ctx, dut, addr)
    assert ctx.get(dut.done) == False

    # first bump alloc
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)
    assert_allocated_at(0)

    # second bump alloc
    await ctx.tick()
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)
    assert_allocated_at(1)

    # unset done
    await ctx.tick()
    assert ctx.get(dut.done) == False
    await ctx.tick().repeat(5)
    assert ctx.get(dut.done) == False

  sim.add_testbench(bench)
  with sim.write_vcd("obj_dir/test_bump_alloc.vcd"):
    sim.run()

def test_alloc_free():
  dut, sim = setup()

  async def bench(ctx: SimulatorContext):
    assert_allocated_at = lambda addr: assert_allocated_at_(ctx, dut, addr)

    # allocate
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)
    assert_allocated_at(0)

    # free
    ctx.set(dut.req_free, True)
    ctx.set(dut.free_addr, 0)
    await ctx.tick()
    ctx.set(dut.req_free, False)

    # free completes in 2 cycles
    assert ctx.get(dut.done) == False
    await ctx.tick()
    assert ctx.get(dut.done) == True

    # realloc
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)

    # realloc completes in 2 cycles
    assert ctx.get(dut.done) == False
    await ctx.tick()
    assert_allocated_at(0)

  sim.add_testbench(bench)
  with sim.write_vcd("obj_dir/test_alloc_free.vcd"):
    sim.run()

def test_many_reallocs():
  dut, sim = setup()

  async def bench(ctx: SimulatorContext):
    assert_allocated_at = lambda addr: assert_allocated_at_(ctx, dut, addr)

    # allocate twice
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)
    assert_allocated_at(0)

    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)
    assert_allocated_at(1)

    # free addr 0
    ctx.set(dut.req_free, True)
    ctx.set(dut.free_addr, 0)
    await ctx.tick()
    ctx.set(dut.req_free, False)
    await ctx.tick()
    ctx.set(dut.req_free, False)
    assert ctx.get(dut.done) == True

    # realloc should allocate into addr 0
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)
    await ctx.tick()
    assert_allocated_at(0)

    # another alloc should bump alloc into 2
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    ctx.set(dut.req_alloc, False)
    assert_allocated_at(2)

  sim.add_testbench(bench)
  with sim.write_vcd("obj_dir/test_many_reallocs.vcd"):
    sim.run()

def test_oom():
  dut, sim = setup()

  async def bench(ctx: SimulatorContext):
    assert_allocated_at = lambda addr: assert_allocated_at_(ctx, dut, addr)

    # allocate CAPACITY times
    for i in range(CAPACITY):
      ctx.set(dut.req_alloc, True)
      await ctx.tick()
      ctx.set(dut.req_alloc, False)
      assert_allocated_at(i)
      await ctx.tick()

    # the next alloc should OOM
    ctx.set(dut.req_alloc, True)
    await ctx.tick()
    assert ctx.get(dut.done) == True
    assert ctx.get(dut.oom) == True

  sim.add_testbench(bench)
  with sim.write_vcd("obj_dir/test_oom.vcd"):
    sim.run()


test_bump_alloc()
test_alloc_free()
test_many_reallocs()
test_oom()
