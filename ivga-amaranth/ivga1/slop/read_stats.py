"""Read Arty runtime statistics: python -m ivga.slop.read_stats PORT [--watch]."""

import argparse
import fcntl
import json
import math
import os
import re
import select
import sys
import termios
import time
from contextlib import contextmanager

FIELDS = (
  "status",
  "selection",
  "alloc_allocs",
  "alloc_frees",
  "alloc_peak",
  "link_erase",
  "interact_annihilate",
)
STATUS_FLAGS = ("busy", "done", "halted", "timed_out")
UART_SETTLE_SECONDS = 0.05
MAX_FRAME_BYTES = len("ivga1 " + " ".join(f"{key}=00000000" for key in FIELDS) + "\r\n")


def parse_snapshot(line: str | bytes) -> dict[str, int]:
  """Parse one complete ASCII/CRLF ivga1 frame, raising ValueError if invalid.

  Fields may arrive in any order, but each required field must occur once.
  Values are exactly eight lowercase hexadecimal digits (unsigned 32-bit).
  """
  if isinstance(line, bytes):
    try:
      line = line.decode("ascii")
    except UnicodeDecodeError as exc:
      raise ValueError("frame must be ASCII") from exc
  if not line.isascii():
    raise ValueError("frame must be ASCII")
  if not line.startswith("ivga1 "):
    raise ValueError("expected 'ivga1 ' prefix")
  if not line.endswith("\r\n"):
    raise ValueError("frame must end with CRLF")

  snapshot = {}
  for field in line[6:-2].split(" "):
    key, separator, value = field.partition("=")
    if not separator or key not in FIELDS:
      raise ValueError(f"unknown or malformed field: {field!r}")
    if key in snapshot:
      raise ValueError(f"duplicate field: {key}")
    if re.fullmatch(r"[0-9a-f]{8}", value) is None:
      raise ValueError(f"{key} must contain exactly eight lowercase hex digits")
    snapshot[key] = int(value, 16)
  missing = set(FIELDS) - snapshot.keys()
  if missing:
    raise ValueError(f"missing fields: {', '.join(sorted(missing))}")
  return snapshot


@contextmanager
def open_uart(port: str):
  """Own a nonblocking 115200 8N1 raw TTY until exit, then restore it.

  flock is advisory: other serial programs must cooperate with the lock.
  """
  fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
  try:
    try:
      fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
      raise OSError("port is locked; close the other statistics reader") from exc
    original = termios.tcgetattr(fd)
    try:
      attrs = original.copy()
      attrs[6] = original[6].copy()
      attrs[0] = 0  # No input translation or software flow control.
      attrs[1] = 0
      attrs[2] &= ~(
        termios.CSIZE | termios.PARENB | termios.PARODD | termios.CSTOPB
        | getattr(termios, "CRTSCTS", 0)
      )
      attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
      attrs[3] = 0  # No canonical processing, echo, or signal characters.
      attrs[4] = attrs[5] = termios.B115200
      attrs[6][termios.VMIN] = 0
      attrs[6][termios.VTIME] = 0
      termios.tcsetattr(fd, termios.TCSANOW, attrs)
      termios.tcflush(fd, termios.TCIOFLUSH)
      # The FTDI bridge can drop a write immediately after line/FIFO setup.
      # Let configuration settle before submitting the first snapshot request.
      time.sleep(UART_SETTLE_SECONDS)
      yield fd
    finally:
      unwinding = sys.exc_info()[0] is not None
      try:
        termios.tcsetattr(fd, termios.TCSANOW, original)
      except (OSError, termios.error):
        # Unplugged USB devices cannot be restored; preserve the original error.
        if not unwinding:
          raise
  finally:
    os.close(fd)


def _wait(fd: int, deadline: float, *, writing: bool = False):
  remaining = deadline - time.monotonic()
  if remaining <= 0:
    raise TimeoutError("timed out waiting for a complete statistics frame")
  readable, writable, _ = select.select(
    [] if writing else [fd], [fd] if writing else [], [], remaining,
  )
  if not (writable if writing else readable):
    raise TimeoutError("timed out waiting for a complete statistics frame")


def read_snapshot(fd: int, timeout: float = 2.0) -> dict[str, int]:
  """Send '?' and read exactly one bounded frame within a total deadline."""
  deadline = time.monotonic() + timeout
  while True:
    _wait(fd, deadline, writing=True)
    try:
      if os.write(fd, b"?") != 1:
        raise OSError("UART disconnected while sending request")
      break
    except BlockingIOError:
      continue

  frame = bytearray()
  while len(frame) < MAX_FRAME_BYTES:
    _wait(fd, deadline)
    try:
      # Do not consume bytes belonging to a subsequent frame.
      byte = os.read(fd, 1)
    except BlockingIOError:
      continue
    if not byte:
      raise OSError("UART disconnected before a complete statistics frame arrived")
    frame.extend(byte)
    if byte == b"\n":
      return parse_snapshot(bytes(frame))
  raise ValueError(f"statistics frame exceeds {MAX_FRAME_BYTES} bytes")


def _positive_seconds(value: str) -> float:
  try:
    seconds = float(value)
  except ValueError as exc:
    raise argparse.ArgumentTypeError("must be a positive finite number of seconds") from exc
  if not math.isfinite(seconds) or seconds <= 0:
    raise argparse.ArgumentTypeError("must be a positive finite number of seconds")
  return seconds


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description="Read Arty USB-UART runtime statistics")
  _ = parser.add_argument("port", metavar="PORT", help="TTY device, e.g. /dev/ttyUSB1")
  _ = parser.add_argument("--watch", action="store_true", help="poll until Ctrl-C")
  _ = parser.add_argument("--interval", type=_positive_seconds, default=1.0, metavar="SECONDS")
  _ = parser.add_argument("--timeout", type=_positive_seconds, default=2.0, metavar="SECONDS")
  args = parser.parse_args(argv)

  try:
    with open_uart(args.port) as fd:
      while True:
        snapshot = read_snapshot(fd, args.timeout)
        output = {
          **snapshot,
          "status_flags": {
            name: bool(snapshot["status"] & (1 << bit))
            for bit, name in enumerate(STATUS_FLAGS)
          },
          "selection_bits": [
            bool(snapshot["selection"] & (1 << bit)) for bit in range(4)
          ],
        }
        print(json.dumps(output, indent=2), flush=True)
        if not args.watch:
          return 0
        time.sleep(args.interval)
  except KeyboardInterrupt:
    print("Stopped; UART settings restored where the device is still connected.", file=sys.stderr)
    return 130
  except TimeoutError as exc:
    print(
      f"error: {args.port}: {exc}. Check the port, FPGA ivga1 firmware, and USB cable; "
      "try a longer --timeout.", file=sys.stderr,
    )
  except ValueError as exc:
    print(
      f"error: {args.port}: invalid statistics frame: {exc}. "
      "Check that the FPGA runs the ivga1 firmware at 115200 baud.", file=sys.stderr,
    )
  except (OSError, termios.error) as exc:
    print(
      f"error: {args.port}: UART I/O failed: {exc}. "
      "Check the connection and device permissions; close other serial monitors.",
      file=sys.stderr,
    )
  return 1


if __name__ == "__main__":
  sys.exit(main())
