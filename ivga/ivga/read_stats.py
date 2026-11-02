"""Read production hardware snapshots: python -m ivga.read_stats PORT [--watch].

Firmware sends snapshots automatically on faults and completion. A '?' requests
another coherent snapshot; requests during transmission coalesce in hardware.
No fixed field inventory is imposed: all exported counters and statuses survive.
"""

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

MAX_FRAME_BYTES = 4096
UART_SETTLE_SECONDS = 0.05
_FRAME_MARKER = b"ivga2"
_KEY = re.compile(r"[a-z][a-z0-9_]*")
_VALUE = re.compile(r"[0-9a-f]{8}")


def parse_snapshot(line: str | bytes) -> dict[str, int]:
  """Parse one complete ASCII ivga2 frame, including its CRLF terminator.

  At least one field is required. Names must be unique, and values must be
  exactly eight lowercase hexadecimal digits representing unsigned 32-bit ints.
  The 4096-byte limit includes the prefix, separators, and CRLF.
  Invalid frames raise ValueError; unknown field names are preserved.
  """
  if len(line) > MAX_FRAME_BYTES:
    raise ValueError(f"statistics frame exceeds {MAX_FRAME_BYTES} bytes")
  if isinstance(line, bytes):
    try:
      line = line.decode("ascii")
    except UnicodeDecodeError as exc:
      raise ValueError("frame must be ASCII") from exc
  if not line.isascii():
    raise ValueError("frame must be ASCII")
  if not line.startswith("ivga2 "):
    raise ValueError("expected 'ivga2 ' prefix")
  if not line.endswith("\r\n"):
    raise ValueError("frame must end with CRLF")
  if not line[6:-2]:
    raise ValueError("frame must contain at least one field")

  snapshot = {}
  for field in line[6:-2].split(" "):
    key, separator, value = field.partition("=")
    if not separator or _KEY.fullmatch(key) is None:
      raise ValueError(f"malformed field: {field!r}")
    if key in snapshot:
      raise ValueError(f"duplicate field: {key}")
    if _VALUE.fullmatch(value) is None:
      raise ValueError(f"{key} must contain exactly eight lowercase hex digits")
    snapshot[key] = int(value, 16)
  return snapshot


@contextmanager
def open_uart(port: str):
  """Own a raw, nonblocking 115200 8N1 TTY and restore settings on exit.

  The exclusive flock is advisory: other programs must cooperate with it.
  Queued input is not flushed, since it may contain an automatic fault report.
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
      attrs[0] = 0
      attrs[1] = 0
      attrs[2] &= ~(
        termios.CSIZE | termios.PARENB | termios.PARODD | termios.CSTOPB
        | getattr(termios, "CRTSCTS", 0)
      )
      attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
      attrs[3] = 0
      attrs[4] = attrs[5] = termios.B115200
      attrs[6][termios.VMIN] = 0
      attrs[6][termios.VTIME] = 0
      termios.tcsetattr(fd, termios.TCSANOW, attrs)
      # FTDI bridges can drop a request immediately after line setup.
      time.sleep(UART_SETTLE_SECONDS)
      yield fd
    finally:
      unwinding = sys.exc_info()[0] is not None
      try:
        termios.tcsetattr(fd, termios.TCSANOW, original)
      except (OSError, termios.error):
        # An unplugged UART cannot be restored; keep the original exception.
        if not unwinding:
          raise
  finally:
    os.close(fd)


def _wait(fd: int, deadline: float, *, writing: bool = False):
  remaining = deadline - time.monotonic()
  if remaining <= 0:
    raise TimeoutError("timed out waiting for a complete ivga2 statistics frame")
  readable, writable, _ = select.select(
    [] if writing else [fd], [fd] if writing else [], [], remaining,
  )
  if not (writable if writing else readable):
    raise TimeoutError("timed out waiting for a complete ivga2 statistics frame")


def _read_frame(fd: int, deadline: float) -> dict[str, int]:
  """Discard leading junk, then strictly parse one frame without reading ahead."""
  frame = bytearray()
  matched = 0
  while True:
    _wait(fd, deadline)
    try:
      # Leave subsequent automatic snapshots queued for the next call.
      byte = os.read(fd, 1)
    except BlockingIOError:
      continue
    if not byte:
      raise OSError("UART disconnected before a complete statistics frame arrived")
    if not frame:
      # The marker has no overlapping prefix/suffix, so only 'i' can restart it.
      if byte == _FRAME_MARKER[matched:matched + 1]:
        matched += 1
      else:
        matched = int(byte == _FRAME_MARKER[:1])
      if matched == len(_FRAME_MARKER):
        frame.extend(_FRAME_MARKER)
      continue
    frame.extend(byte)
    if byte == b"\n":
      # Once synchronized, malformed frames are errors, not disposable noise.
      return parse_snapshot(bytes(frame))
    if len(frame) >= MAX_FRAME_BYTES:
      raise ValueError(f"statistics frame exceeds {MAX_FRAME_BYTES} bytes")


def read_snapshot(fd: int, timeout: float = 2.0) -> dict[str, int]:
  """Send '?' and return the next ivga2 snapshot within one total deadline.

  The returned snapshot may be an unsolicited fault/completion report, not a
  response to this particular request. Leading partial/noisy data is discarded;
  malformed complete ivga2 frames raise ValueError. The caller must provide a
  nonblocking raw UART (normally from open_uart) and exclusively own its I/O.
  """
  if not math.isfinite(timeout) or timeout <= 0:
    raise ValueError("timeout must be a positive finite number of seconds")
  deadline = time.monotonic() + timeout
  while True:
    _wait(fd, deadline, writing=True)
    try:
      if os.write(fd, b"?") != 1:
        raise OSError("UART disconnected while sending request")
      break
    except BlockingIOError:
      continue
  return _read_frame(fd, deadline)


def _positive_seconds(value: str) -> float:
  try:
    seconds = float(value)
  except ValueError as exc:
    raise argparse.ArgumentTypeError("must be a positive finite number of seconds") from exc
  if not math.isfinite(seconds) or seconds <= 0:
    raise argparse.ArgumentTypeError("must be a positive finite number of seconds")
  return seconds


def _print_snapshot(snapshot: dict[str, int]):
  output: dict[str, int | list[str]] = dict(snapshot)
  # Decoding is additive: never overwrite an arbitrary hardware field.
  if "active_faults" not in output and any(key.startswith("fault_") for key in snapshot):
    output["active_faults"] = sorted(
      key for key, value in snapshot.items() if key.startswith("fault_") and value
    )
  print(json.dumps(output, indent=2, sort_keys=True), flush=True)


def main(argv=None) -> int:
  parser = argparse.ArgumentParser(description="Read production ivga2 USB-UART statistics")
  _ = parser.add_argument("port", metavar="PORT", help="TTY device, e.g. /dev/ttyUSB1")
  _ = parser.add_argument(
    "--watch", action="store_true", help="listen for automatic snapshots and poll until Ctrl-C",
  )
  _ = parser.add_argument("--interval", type=_positive_seconds, default=1.0, metavar="SECONDS")
  _ = parser.add_argument("--timeout", type=_positive_seconds, default=2.0, metavar="SECONDS")
  args = parser.parse_args(argv)

  try:
    with open_uart(args.port) as fd:
      _print_snapshot(read_snapshot(fd, args.timeout))
      if not args.watch:
        return 0
      next_request = time.monotonic() + args.interval
      while True:
        remaining = next_request - time.monotonic()
        if remaining <= 0:
          snapshot = read_snapshot(fd, args.timeout)
          next_request = time.monotonic() + args.interval
        else:
          readable, _, _ = select.select([fd], [], [], remaining)
          if not readable:
            continue
          snapshot = _read_frame(fd, time.monotonic() + args.timeout)
        _print_snapshot(snapshot)
  except KeyboardInterrupt:
    print("Stopped; UART settings restored where the device is still connected.", file=sys.stderr)
    return 130
  except TimeoutError as exc:
    print(
      f"error: {args.port}: {exc}. Check the port, FPGA ivga2 firmware, and USB cable; "
      "try a longer --timeout.", file=sys.stderr,
    )
  except ValueError as exc:
    print(
      f"error: {args.port}: invalid statistics frame: {exc}. "
      "Check that the FPGA runs the ivga2 firmware at 115200 baud.", file=sys.stderr,
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
