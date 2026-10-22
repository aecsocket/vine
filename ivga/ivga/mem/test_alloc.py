from amaranth import *
from amaranth import Cover
from ivga.mem.alloc import Alloc
from ivga.verify import Verify


def setup():
  dut = Alloc(capacity=4)
  verify = Verify(dut, cycles=5)
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

  # on the first cycle, alloc must be reset
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
  m.d.comb += Cover((cycle == 1) & dut.done)

  verify.prove()

verify_one_bump_alloc()

def verify_two_bump_allocs():
  dut, verify = setup()
  m, cycle = verify.m, verify.cycle

  # request 1st alloc
  with m.If(cycle == 0):
    m.d.comb += dut.req_alloc.eq(True)
  with m.If(cycle == 1):
    assert_bump_allocd(m, dut, at=0)
  m.d.comb += Cover((cycle == 1) & dut.done)

  # request 2nd alloc after a time gap
  with m.If(cycle == 3):
    m.d.comb += dut.req_alloc.eq(True)
  with m.If(cycle == 4):
    assert_bump_allocd(m, dut, at=1)
  m.d.comb += Cover((cycle == 4) & dut.done)

  verify.prove()

verify_two_bump_allocs()
