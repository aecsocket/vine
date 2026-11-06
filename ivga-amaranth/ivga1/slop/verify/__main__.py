"""Run the symbolic formal suite using native Yosys, SMTBMC and Z3."""

import argparse
import fnmatch
import json
import shutil

from . import alloc, arbiter, flow, grafter, linker, memory, semantics
from .core import OUTPUT, run_case


def _positive(value):
  seconds = int(value)
  if seconds <= 0:
    raise argparse.ArgumentTypeError("must be positive")
  return seconds


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  _ = parser.add_argument("patterns", nargs="*", help="case-name globs, e.g. 'memory_*'")
  _ = parser.add_argument("--list", action="store_true", help="list cases without running tools")
  _ = parser.add_argument("--slow", action="store_true", help="include costly allocator/wire checks (some remain unverified)")
  _ = parser.add_argument("--regressions", action="store_true", help="include known failing commute specifications")
  _ = parser.add_argument("--timeout", type=_positive, help="override the per-phase solver timeout in seconds")
  _ = parser.add_argument("--fail-fast", action="store_true")
  args = parser.parse_args()

  catalog = [case for group in (arbiter, memory, alloc, flow, grafter, linker, semantics) for case in group.cases()]
  if args.regressions:
    catalog += semantics.regression_cases()
  names = [case.name for case in catalog]
  if len(names) != len(set(names)):
    raise RuntimeError("Duplicate formal case names would overwrite artifacts")
  selected = [case for case in catalog if (args.slow or not case.slow)
              and (not args.patterns or any(fnmatch.fnmatchcase(case.name, pattern) for pattern in args.patterns))]
  if not selected:
    parser.error("no matching cases; use --list --slow or --regressions to include optional checks")
  if args.list:
    for case in selected:
      label = "prove" if case.mode == "prove" else "bounded"
      print(f"{case.name}: {label}, depth={case.depth}, covers={case.cover}, timeout={case.timeout}s"
            + (", SLOW" if case.slow else ""))
    print(f"{len(selected)} cases")
    return 0
  for tool in ("yosys", "yosys-smtbmc", "z3"):
    if shutil.which(tool) is None:
      parser.error(f"required tool not found: {tool}; install native Yosys/SMTBMC and Z3")

  results = []
  try:
    for index, case in enumerate(selected, 1):
      print(f"[{index}/{len(selected)}] {case.name}", flush=True)
      result = run_case(case, timeout=args.timeout)
      results.append(result)
      print(f"  {'PASS' if result.passed else 'FAIL'}: {result.detail} ({result.seconds:.2f}s)", flush=True)
      if not result.passed and args.fail_fast:
        break
  except KeyboardInterrupt:
    print("Interrupted; solver process group terminated.", flush=True)
    return 130

  proved = sum(result.passed and result.case.mode == "prove" for result in results)
  bounded = sum(result.passed and result.case.mode == "bmc" for result in results)
  failed = sum(not result.passed for result in results)
  unrun = len(selected) - len(results)
  summary = {
    "proved": proved, "bounded_passes": bounded, "failed": failed, "not_run": unrun,
    "excluded_slow_cases": [case.name for case in catalog if case.slow and not args.slow],
    "known_failure_regressions_included": args.regressions,
    "results": [{"name": result.case.name, "mode": result.case.mode, "depth": result.case.depth,
                 "covers_checked": result.case.cover, "passed": result.passed,
                 "seconds": result.seconds, "detail": result.detail} for result in results],
  }
  OUTPUT.mkdir(parents=True, exist_ok=True)
  _ = (OUTPUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
  print(f"\n{proved} proved; {bounded} bounded passes; {failed} failed; {unrun} not run")
  print(f"Logs, counterexamples and summary: {OUTPUT}")
  return int(bool(failed or unrun))


if __name__ == "__main__":
  raise SystemExit(main())
