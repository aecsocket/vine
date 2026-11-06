import argparse
import os
import subprocess
import sys
from pathlib import Path


def run(command, root):
    build = root / "build"
    if command == "program":
        # Never rebuild: program exactly the bitstream the user inspected.
        if not (build / "ivga.bit").is_file():
            raise FileNotFoundError("Missing build/ivga.bit; run swim plugin vivado first")
    elif not (build / "spade.sv").is_file():
        raise FileNotFoundError("Missing build/spade.sv; run swim build first")

    if command in ("vivado", "program"):
        executable = os.environ.get("VIVADO", str(Path.home() / ".local/bin/vivado"))
        action = "build" if command == "vivado" else "program"
        _ = subprocess.run(
            [executable, "-mode", "batch", "-source", str(root / "board" / f"{action}.tcl"),
             "-log", f"vivado-{action}.log", "-journal", f"vivado-{action}.jou"],
            cwd=build, check=True,
        )
    else:
        executable = os.environ.get("VERILATOR", "verilator")
        # Use Yosys's functional Xilinx model, not a handwritten RTL wrapper.
        cells = Path(os.environ.get("XILINX_CELLS_SIM", "/usr/share/yosys/xilinx/cells_sim.v"))
        if not cells.is_file():
            raise FileNotFoundError(
                "Missing Xilinx simulation library; install yosys or set XILINX_CELLS_SIM"
            )
        _ = subprocess.run(
            [executable, "--cc", "--exe", "--build", "--top-module", "top",
             "-Wno-fatal", "--Mdir", "obj_dir", "-o", "blinker-test",
             str(build / "spade.sv"), "-v", str(cells.resolve()),
             str(root / "test/blinker.cpp")],
            cwd=build, check=True,
        )
        _ = subprocess.run([str(build / "obj_dir/blinker-test")], cwd=build, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("vivado", "program", "test"))
    _ = parser.add_argument("project_root", type=Path)
    args = parser.parse_args(argv)
    try:
        run(args.command, args.project_root.resolve())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"{args.command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
