"""Negative controls: ensure the runner rejects bugs and vacuous environments."""

from amaranth import Assert, Assume, Const, Cover, Module

from .core import OUTPUT, FormalCase, run_case


def _bad_assertion():
  m = Module()
  m.d.comb += Assert(Const(0))
  return m


def _contradiction():
  m = Module()
  m.d.comb += [Assume(Const(0)), Assert(Const(1))]
  return m


def _unreachable_cover():
  m = Module()
  m.d.comb += [Assert(Const(1)), Cover(Const(0))]
  return m


def main():
  controls = [
    (FormalCase("runner_bad_assertion", _bad_assertion, depth=3, mode="bmc", cover=False),
     "bmc", "Assert failed"),
    (FormalCase("runner_contradictory_assumptions", _contradiction, depth=3, mode="bmc", cover=False),
     "bmc", "unsatisfiable"),
    (FormalCase("runner_unreachable_cover", _unreachable_cover, depth=3, mode="bmc"),
     "cover", "Unreached cover"),
  ]
  for case, phase, marker in controls:
    result = run_case(case)
    assert not result.passed, f"Runner incorrectly accepted {case.name}"
    assert result.detail.startswith(f"{phase} failed"), result.detail
    text = (OUTPUT / case.name / f"{phase}.log").read_text()
    assert marker in text, text
    print(f"PASS negative control: {case.name} rejected for the expected reason")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
