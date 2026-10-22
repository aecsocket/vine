import subprocess

from amaranth.back import rtlil

from .counter import UpCounter

OUT_DIR = "obj_dir"
TARGET = "counter"

dut = UpCounter(100)
with open(f"{OUT_DIR}/{TARGET}.il", "w", encoding="utf-8") as file:
    _ = file.write(rtlil.convert(
        dut,
        name=TARGET,
        ports=[dut.enable, dut.overflow]
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
