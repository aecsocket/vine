import argparse

from amaranth_boards.arty_a7 import ArtyA7_100Platform

from ivga.top import Processor

BUILD_DIR = "obj_dir/arty_a7_100t"

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
    help="Upload the built bitstream to the FPGA"
  )

  args = parser.parse_args()

  processor = Processor()

  if args.command == "build":
    ArtyA7_100Platform().build(
      processor,
      build_dir=BUILD_DIR,
      do_program=args.program
    )


if __name__ == "__main__":
  main()
