import subprocess

from amaranth import *
from amaranth.back import rtlil
from amaranth.lib.memory import Memory

from ivga.mem.alloc import Alloc
from ivga.ty import ADDR_WIDTH, SYNC, Word

OUT_DIR = "obj_dir"
TARGET = "counter"

dut = Alloc(capacity=128)
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
  read.en.eq(dut.heap.read_enable),
  read.addr.eq(dut.heap.read_addr),
  dut.heap.read_data.eq(read.data),

  write.en.eq(dut.heap.write_enable),
  write.addr.eq(dut.heap.write_addr),
  write.data.eq(dut.heap.write_data),
]

with open(f"{OUT_DIR}/{TARGET}.il", "w", encoding="utf-8") as file:
    _ = file.write(rtlil.convert(
        m,
        name=TARGET,
        ports=[dut.req_alloc, dut.req_free, dut.free_addr, dut.alloc_addr, dut.done, dut.oom],
    ))

_ = subprocess.run(
    [
        "yosys",
        "-p",
        (
            f"read_rtlil {OUT_DIR}/{TARGET}.il; "
            "synth_xilinx -family xc7 -top counter; "
            "stat; "
            f"write_verilog -noattr {OUT_DIR}/{TARGET}.v"
        ),
    ],
    check=True,
)

_ = subprocess.run(
    [
        "yosys",
        "-p",
        (
            f"read_rtlil {OUT_DIR}/{TARGET}.il; "
            "hierarchy -top counter; "
            "proc; "
            "opt; "
            f"show -format svg -prefix {OUT_DIR}/{TARGET} {TARGET}"
        )
    ],
    check=True,
)
