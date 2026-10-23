!!! EVERYTHING IN THIS DIR IS AI GENERATED !!!

i just wanted to get my board workin quickly.

# Generated testing and diagnostics

This package contains the generated board harness, UART statistics transport,
host reader, and test suites. Core hardware remains in `ivga.mem` and
`ivga.runtime`; the existing formal runner remains in `ivga.verify`.

Run commands from the repository's `ivga/` directory:

```sh
python -m ivga.main --slop build
python -m ivga.main --slop program
python -m ivga.slop.read_stats /dev/ttyUSB1
python -m ivga.slop.read_stats /dev/ttyUSB1 --watch --interval 0.5
```

The harness uses `build/arty_a7_100t_slop`. UART is 115200 baud, 8N1. Each `?`
request returns an atomic counter/status snapshot. The reader waits 50 ms after
serial-port configuration so the USB-UART bridge can settle. The UART device
number may change; identify it under `/dev/serial/by-id/`.

BTN0 starts; BTN1 resets; BTN2 halts; BTN3 emits a snapshot. LED0 is sticky
completion; LED1 is sticky fault/halt. SW0 selects empty/eraser auxiliaries,
SW1 injects an unsupported label mismatch, and SW2..3 select the address bank.
Counters reset on Start/reset and include setup allocations. Peak means the
allocator's bump-pointer watermark, not peak live allocations.

Tests:

```sh
python -m unittest ivga.slop.test_uart ivga.slop.test_top_slop ivga.slop.test_read_stats
python -m ivga.slop.test_link
python -m ivga.slop.test_alloc
python -m ivga.slop.test_stack
```

Build-output locations are unchanged: linker simulation VCDs go under
`build/runtime/test_link`, and formal artifacts go under `build/verify`.
