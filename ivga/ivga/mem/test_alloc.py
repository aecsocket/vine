from amaranth import *
from ivga.mem.alloc import Alloc
from ivga.verify import Verify


def setup():
  dut = Alloc(capacity=4)
  verify = Verify(dut, cycles=3)
  verify.m.d.comb += [
    dut.req_free.eq(False),
    dut.free_addr.eq(0),
    dut.heap.done.eq(0),
    dut.heap.read_data.eq(0),
  ]
  return dut, verify

def assert_bump_allocd(m, dut: Alloc, at: int):
  m.d.comb += [
    Assert(dut.done),
    Assert(~dut.oom),
    Assert(dut.empty == at + 1),
    Assert(dut.alloc_addr == at),
  ]

def verify_one_bump_alloc():
  dut, verify = setup()
  m, cycle = verify.m, verify.cycle

  # on the first cycle..
  with m.If(cycle == 0):
    m.d.comb += [
      Assert(dut.empty == 0),
      Assert(~dut.done),
    ]

  # request allocation on the first cycle
  with m.If(cycle == 0):
    m.d.comb += dut.req_alloc.eq(True)
  with m.If(cycle == 1):
    assert_bump_allocd(m, dut, at=0)

  verify.prove("verify_one_bump_alloc")

verify_one_bump_alloc()
