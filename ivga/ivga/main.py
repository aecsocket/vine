import argparse
import subprocess
from pathlib import Path

from amaranth.build.run import LocalBuildProducts
from amaranth_boards.arty_a7 import ArtyA7_100Platform

from ivga.top import Processor

BUILD_DIR = "build/arty_a7_100t"

def main():
  parser = argparse.ArgumentParser(
    description="Toolchain for the FPGA design",
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
  processor = Processor()

  if args.command == "build":
    platform.build(
      processor,
      build_dir=BUILD_DIR,
      do_program=args.program
    )

  elif args.command == "util":
    _ = subprocess.run(
      [
        "less",
        "--quit-if-one-screen",
        str(Path(BUILD_DIR) / "top_utilization_place.rpt")
      ],
      check=True,
    )

  elif args.command == "program":
    platform.toolchain_program(
      LocalBuildProducts(BUILD_DIR),
      name="top",
    )


if __name__ == "__main__":
  main()
