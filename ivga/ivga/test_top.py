import argparse
import json
from pathlib import Path

from amaranth.sim import Simulator

from ivga import Port
from ivga.top import Processor

parser = argparse.ArgumentParser(description="Simulate a BRAM-initialized Processor")
_ = parser.add_argument("image", type=Path, nargs="?", default=Path("build/fib.hex"))
args = parser.parse_args()
manifest = json.loads(args.image.with_suffix(".json").read_text())
words = [int(line, 16) for line in args.image.read_text().splitlines()]
dut = Processor(reserved_pages=len(manifest["reserved_pages"]))
dut.runtime.memory.memory.init = words

async def testbench(ctx):
  ctx.set(dut.entry_addr, manifest["entry_addr"])
  ctx.set(dut.root_port, Port.eraser())
  ctx.set(dut.start, True)
  await ctx.tick()
  ctx.set(dut.start, False)

  print("started")
  print("\n\n\n\n", end="")
  cycles = 0
  while not ctx.get(dut.done):
    await ctx.tick()
    cycles += 1
    virtual_time_ms = cycles / 100_000.0

    pages_reserved = ctx.get(dut.runtime.page_alloc.reserved_pages)
    pages_allocd = ctx.get(dut.runtime.page_alloc.stat_allocs)
    pages_freed = ctx.get(dut.runtime.page_alloc.stat_frees)

    pairs_allocd = ctx.get(dut.runtime.pair_alloc.stat_allocs)
    pairs_freed = ctx.get(dut.runtime.pair_alloc.stat_frees)

    annihilate = ctx.get(dut.runtime.interact.stat_annihilate)
    copy = ctx.get(dut.runtime.interact.stat_copy)
    commute = ctx.get(dut.runtime.interact.stat_commute)
    graft = ctx.get(dut.runtime.interact.stat_graft)
    extrinsic = ctx.get(dut.runtime.interact.stat_extrinsic)

    print(
      "\x1b[4F"
      "\x1b[2K"
      f"cycles: {cycles} | {virtual_time_ms:.4f} ms\n"
      "\x1b[2K"
      f"pages reserved/allocd/freed: {pages_reserved}/{pages_allocd}/{pages_freed}\n"
      "\x1b[2K"
      f"pairs allocd/freed: {pairs_allocd}/{pairs_freed}\n"
      "\x1b[2K"
      f"annihilate/copy/commute/graft/extrinsic: {annihilate}/{copy}/{commute}/{graft}/{extrinsic}\n",
      end="",
      flush=True
    )

  faults = []
  if ctx.get(dut.fault.halt):
    faults.append("halt")
  if ctx.get(dut.fault.oom):
    faults.append("oom")
  if ctx.get(dut.fault.bad_graft):
    faults.append("bad_graft")
  if ctx.get(dut.fault.active_full):
    faults.append("active_full")
  if ctx.get(dut.fault.ext_copy):
    faults.append("ext_copy")
  if ctx.get(dut.fault.ext_inputs):
    faults.append("ext_inputs")
  if ctx.get(dut.fault.ext_generic):
    faults.append("ext_generic")
  if ctx.get(dut.fault.invalid_interaction):
    faults.append("invalid_interaction")
  print(f"done, faults: {faults}")

sim = Simulator(dut)
sim.add_clock(1e-8)
sim.add_testbench(testbench)
with sim.write_vcd("build/test_top.vcd"):
  sim.run()
