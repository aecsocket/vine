"""Small SMTBMC runner; a bounded pass is deliberately not called a proof.

Builders return an Elaboratable without defining sync. The runner supplies a
resetless sync domain: properties start at configuration-time initialization.
Runtime-reset behavior is not covered unless a harness explicitly models it.
"""

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from amaranth import ClockDomain, Elaboratable, Module
from amaranth.back import rtlil

OUTPUT = Path(__file__).resolve().parents[3] / "build" / "slop_verify"


@dataclass(frozen=True)
class FormalCase:
  name: str
  build: Callable[[], Elaboratable]
  depth: int = 20
  mode: Literal["prove", "bmc"] = "prove"
  cover: bool = True
  timeout: int = 30
  slow: bool = False


@dataclass(frozen=True)
class Result:
  case: FormalCase
  passed: bool
  seconds: float
  detail: str


def _command(args: list[str], directory: Path, phase: str, timeout: int):
  for suffix in (".vcd", "_tb.v"):
    (directory / f"{phase}{suffix}").unlink(missing_ok=True)
  with (directory / f"{phase}.log").open("w") as log, subprocess.Popen(
    args, cwd=directory, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
  ) as process:
    try:
      code = process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
      # SMTBMC starts a solver child; terminate the whole group, not just its parent.
      try:
        os.killpg(process.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      _ = process.wait()
      if isinstance(exc, KeyboardInterrupt):
        raise
      raise RuntimeError(f"{phase} timed out after {timeout}s; see {directory / f'{phase}.log'}") from exc
  if code:
    raise RuntimeError(f"{phase} failed (exit {code}); see {directory / f'{phase}.log'}")
  if phase == "induction":
    # SMTBMC can dump an intermediate failed induction attempt before a larger
    # k succeeds. It is not a reachable DUT counterexample after a passing proof.
    (directory / "induction.vcd").unlink(missing_ok=True)


def run_case(case: FormalCase, *, timeout: int | None = None) -> Result:
  start = time.monotonic()
  directory = OUTPUT / case.name
  directory.mkdir(parents=True, exist_ok=True)
  limit = case.timeout if timeout is None else timeout
  try:
    m = Module()
    domain = ClockDomain("sync", reset_less=True)
    m.domains.sync = domain
    m.submodules.properties = case.build()
    _ = (directory / "design.il").write_text(rtlil.convert(m, name="top", ports=[domain.clk]))
    script = (
      "read_rtlil design.il; prep -top top; memory_nordff; memory_collect; opt_clean; "
      "async2sync; chformal -lower; dffunmap; write_smt2 -wires design.smt2"
    )
    _command(["yosys", "-Q", "-T", "-p", script], directory, "prepare", limit)
    phases = [("bmc", [])]
    if case.mode == "prove":
      phases.append(("induction", ["-i"]))
    if case.cover:
      phases.append(("cover", ["-c"]))
    for phase, flags in phases:
      _command([
        "yosys-smtbmc", "-s", "z3", "-t", str(case.depth), "--presat", "--noprogress",
        "--dump-vcd", f"{phase}.vcd",
        *flags, "design.smt2",
      ], directory, phase, limit)
    detail = "PROVED (base + induction)" if case.mode == "prove" else f"BOUNDED ({case.depth} steps)"
    if case.cover:
      detail += "; covers reached"
    return Result(case, True, time.monotonic() - start, detail)
  except (OSError, RuntimeError, ValueError, AssertionError, TypeError, AttributeError) as exc:
    return Result(case, False, time.monotonic() - start, str(exc))
