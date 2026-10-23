import argparse
import subprocess
from pathlib import Path

from amaranth.build.run import LocalBuildProducts
from amaranth_boards.arty_a7 import ArtyA7_100Platform

from ivga.slop.top_slop import Processor as SlopProcessor
from ivga.top import Processor

BUILD_DIR = "build/arty_a7_100t"
SLOP_BUILD_DIR = "build/arty_a7_100t_slop"

def main():
  parser = argparse.ArgumentParser(
    description="Toolchain for the FPGA design",
  )
  _ = parser.add_argument(
    "--slop", action="store_true",
    help="Use the interactive statistics harness instead of top.py",
  )
  subparsers = parser.add_subparsers(dest="command", required=True)

  build = subparsers.add_parser(
    "build",
    help="Run the full build chain",
  )
  _ = build.add_argument(
    "--program",
    action="store_true",
    help="Upload the built bitstream to the FPGA",
  )

  _ = subparsers.add_parser(
    "util",
    help="See utilization report (RAM usage etc.) of the built design",
  )

  _ = subparsers.add_parser(
    "program",
    help="Upload the built bitstream to the FPGA",
  )

  args = parser.parse_args()

  platform = ArtyA7_100Platform()
  build_dir = SLOP_BUILD_DIR if args.slop else BUILD_DIR

  if args.command == "build":
    platform.build(
      SlopProcessor() if args.slop else Processor(),
      build_dir=build_dir,
      do_program=args.program
    )

  elif args.command == "util":
    _ = subprocess.run(
      [
        "less",
        "--quit-if-one-screen",
        str(Path(build_dir) / "top_utilization_place.rpt")
      ],
      check=True,
    )

  elif args.command == "program":
    platform.toolchain_program(
      LocalBuildProducts(build_dir),
      name="top",
    )


if __name__ == "__main__":
  main()
