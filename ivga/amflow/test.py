from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.memory import Memory
from amaranth.lib.wiring import In, Out
from amaranth.sim import Simulator

from amflow import Flow, expand, sync, tick
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

  _ = sync(f, dummy.done.eq(True))
  yield tick(f)


def expanded_ver_of_do_something(m: Module, dummy: Dummy):
  read_6 = Signal(8)

  with m.FSM(init="init_6"):
    with m.State("init_6"):
      m.d.comb += [
        dummy.memory_write.en.eq(True),
        dummy.memory_write.addr.eq(6),
        dummy.memory_write.data.eq(0x42),
      ]
      m.next = "init_7"
    with m.State("init_7"):
      m.d.comb += [
        dummy.memory_write.en.eq(True),
        dummy.memory_write.addr.eq(7),
        dummy.memory_write.data.eq(0x99),
      ]
      m.next = "read_6"
    with m.State("read_6"):
      m.d.comb += [
        dummy.memory_read.en.eq(True),
        dummy.memory_read.addr.eq(6),
      ]
      m.next = "read_7"
    with m.State("read_7"):
      m.d.sync += read_6.eq(dummy.memory_read.data)
      m.d.comb += [
        dummy.memory_read.en.eq(True),
        dummy.memory_read.addr.eq(7),
      ]
      m.next = "if"
    with m.State("if"):
      with m.If(read_6 == dummy.memory_read.data):
        # MERGED WITH NEXT OP
        m.d.comb += [
          dummy.memory_write.en.eq(True),
          dummy.memory_write.addr.eq(8),
          dummy.memory_write.data.eq(read_6),
        ]
        m.next = "set_done"
      with m.Else():
        # MERGED WITH NEXT OP
        m.d.comb += [
          dummy.memory_write.en.eq(True),
          dummy.memory_write.addr.eq(8),
          dummy.memory_write.data.eq(dummy.memory_read.data),
        ]
        m.next = "set_done"
    with m.State("set_done"):
      m.d.sync += dummy.done.eq(True)
      m.next = "init_6"


async def dummy_testbench(ctx):
  await ctx.tick().repeat(10)


dut = Dummy()
sim = Simulator(dut)
sim.add_clock(1e-6)
sim.add_testbench(dummy_testbench)
with sim.write_vcd("build/test_amflow.vcd"):
  sim.run()
