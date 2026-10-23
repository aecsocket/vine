import subprocess
import sys
from pathlib import Path

from amaranth import *
from amaranth.back import rtlil

from ivga import SYNC

OUTPUT_DIR = Path("build") / "verify"
DESIGN_IL = "design.il"
DESIGN_SMT2 = "design.smt2"

class Verify:
  def __init__(self, dut, cycles: int):
    self.m = Module()
    self.domain = ClockDomain(SYNC, reset_less=True)
    self.m.domains.sync = self.domain
    self.m.submodules.dut = dut

    self.cycle = Signal(range(cycles + 1))
    with self.m.If(self.cycle < cycles):
      self.m.d.sync += self.cycle.eq(self.cycle + 1)

  def prove(self, name: str | None = None, depth: int = 6):
    # get the verification test name if unspecified
    if name is None:
      name = sys._getframe(1).f_code.co_name  # pyright: ignore[reportPrivateUsage]

    # make the Yosys IL and write it to a file
    output_dir = Path(__file__).resolve().parent.parent.parent / OUTPUT_DIR / name
    output_dir.mkdir(parents=True, exist_ok=True)
    il = rtlil.convert(self.m, name="top", ports=[self.domain.clk])
    _ = (output_dir / DESIGN_IL).write_text(il)

    _ = subprocess.run(
      [
        "yosys",
        "-Q", # quiet banner
        "-T", # quiet footer
        "-p", # run commands
        (
          f"read_rtlil {DESIGN_IL}; "
          "prep -top top; "
          "chformal -lower; "
          "memory_map; "
          "opt; "
          "async2sync; "
          "dffunmap; "
          f"write_smt2 -wires {DESIGN_SMT2}"
        )
      ],
      cwd=output_dir,
      check=True,
      timeout=30,
    )

    run_phase(name, depth, output_dir, "bmc", [])
    run_phase(name, depth, output_dir, "induction", ["-i"])
    run_phase(name, depth, output_dir, "cover", ["-c"])
    print(f"{name}: verification complete")


def run_phase(name: str, depth: int, output_dir: Path, phase: str, args):
  print(f"{name}: running {phase}")
  _ = subprocess.run(
    [
      "yosys-smtbmc",
      "-s", "z3",
      "-t", str(depth),
      "--presat",
      "--noprogress",
      "--dump-vcd", f"{name}-{phase}.vcd",
      *args,
      DESIGN_SMT2,
    ],
    cwd=output_dir,
    check=True,
    timeout=30,
  )
  print(f"{name}: finished {phase}")
