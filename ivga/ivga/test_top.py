import argparse
import json
from pathlib import Path

from amaranth.sim import Simulator

from ivga import Port, Tag
from ivga.extrinsic import TY_IDS
from ivga.extrinsic.n32 import N32
from ivga.mem.heap import CAPACITY
from ivga.top import Processor

parser = argparse.ArgumentParser(description="Simulate a BRAM-initialized Processor")
_ = parser.add_argument("image", type=Path, nargs="?", default=Path("build/fib.hex"))
_ = parser.add_argument("--expected", type=int, default=2178309, help="Expected N32 result")
_ = parser.add_argument("--discard", action="store_true", help="Discard the result for long benchmarks")
args = parser.parse_args()
manifest = json.loads(args.image.with_suffix(".json").read_text())
words = [int(line, 16) for line in args.image.read_text().splitlines()]
dut = Processor(reserved_pages=len(manifest["reserved_pages"]))
dut.runtime.memory.memory.init = words

async def testbench(ctx):
  result_addr = None
  if not args.discard:
    ctx.set(dut.pairs.req_alloc, 1)
    while not ctx.get(dut.pairs.granted):
      await ctx.tick()
    await ctx.tick()
    ctx.set(dut.pairs.req_alloc, 0)
    while not ctx.get(dut.pairs.done):
      await ctx.tick()
    assert not ctx.get(dut.pairs.oom), "Could not allocate the result pair"

    result_addr = ctx.get(dut.pairs.alloc_bases[0])
    # Keep this pair allocated so the result survives until we inspect it.
    ctx.set(dut.runtime.memory.memory.data[result_addr], 0)
    ctx.set(dut.runtime.memory.memory.data[result_addr ^ 1], 0)

  ctx.set(dut.entry_addr, manifest["entry_addr"])
  ctx.set(dut.root_port, Port.eraser() if result_addr is None else Port.wire(result_addr))
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

  assert not ctx.get(dut.failed), f"Runtime faulted: {faults}"
  if result_addr is None:
    return
  result = Port.wire(result_addr)
  seen = set()
  while result.tag == Tag.WIRE:
    addr = result.payload
    assert addr < CAPACITY, f"Result wire address out of range: {addr}"
    assert addr not in seen, f"Cycle in result wire chain at address {addr}"
    seen.add(addr)
    result = Port.from_bits(ctx.get(dut.runtime.memory.memory.data[addr]))
  assert result.tag == Tag.EXT_VAL, f"Expected EXT_VAL, got {result.tag}"
  assert result.label == TY_IDS[N32], f"Expected N32 type ID, got {result.label}"
  assert result.payload == args.expected, f"Expected {args.expected}, got {result.payload}"
  print(f"result: {N32}#{result.payload}")

sim = Simulator(dut)
sim.add_clock(1e-8)
sim.add_testbench(testbench)
with sim.write_vcd("build/test_top.vcd"):
  sim.run()
