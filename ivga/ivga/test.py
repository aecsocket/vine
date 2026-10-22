from amaranth.sim import Simulator

from .counter import UpCounter

PERIOD = 0.00_000_001 # 1ns
dut = UpCounter(100)

async def tb_basic(ctx):
    ctx.set(dut.enable, True)

    await ctx.tick().repeat(5)
    assert ctx.get(dut.count == 5)

    ctx.set(dut.enable, False)
    await ctx.tick().repeat(5)
    assert ctx.get(dut.count == 5)
    ctx.set(dut.enable, True)

sim = Simulator(dut)
sim.add_clock(PERIOD)
sim.add_testbench(tb_basic)
with sim.write_vcd("vcds/test.vcd"):
    sim.run_until(PERIOD * 10)
