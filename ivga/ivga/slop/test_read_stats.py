"""Stdlib-only UART tests: python -m unittest ivga.slop.test_read_stats."""

import fcntl
import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import override
from unittest.mock import patch

from ivga.slop import read_stats

FRAME = (
  b"ivga1 status=0000000b selection=00000005 alloc_allocs=0000002a "
  b"alloc_frees=00000010 alloc_peak=ffffffff link_erase=000000ab "
  b"interact_annihilate=12345678\r\n"
)
EXPECTED = {
  "status": 11,
  "selection": 5,
  "alloc_allocs": 42,
  "alloc_frees": 16,
  "alloc_peak": 0xffffffff,
  "link_erase": 171,
  "interact_annihilate": 0x12345678,
}


class ParseSnapshotTests(unittest.TestCase):
  def test_valid_bytes_and_text(self):
    self.assertEqual(read_stats.parse_snapshot(FRAME), EXPECTED)
    self.assertEqual(read_stats.parse_snapshot(FRAME.decode("ascii")), EXPECTED)

  def test_zero_values_and_reordered_fields(self):
    frame = "ivga1 " + " ".join(
      f"{key}=00000000" for key in reversed(read_stats.FIELDS)
    ) + "\r\n"
    self.assertEqual(read_stats.parse_snapshot(frame), dict.fromkeys(EXPECTED, 0))

  def test_reject_invalid_frames(self):
    bad_frames = [
      b"",
      FRAME.replace(b"ivga1 ", b"ivga2 "),
      b" " + FRAME,
      FRAME[6:],
      FRAME[:-2],
      FRAME.replace(b"\r\n", b"\n"),
      FRAME.replace(b"\r\n", b"\r"),
      FRAME + b"junk",
      FRAME + FRAME,
      FRAME.replace(b"alloc_frees=00000010 ", b""),
      FRAME[:-2] + b" status=00000000\r\n",
      FRAME[:-2] + b" mystery=00000000\r\n",
      FRAME.replace(b"status=", b"status:"),
      FRAME.replace(b"status=", b"STATUS="),
      FRAME.replace(b"0000000b", b"0000000B"),
      FRAME.replace(b"0000000b", b"0000000g"),
      FRAME.replace(b"0000000b", b"000000b"),
      FRAME.replace(b"0000000b", b"100000000"),
      FRAME.replace(b"0000000b", b"0x00000b"),
      FRAME.replace(b"0000000b", b"-000000b"),
      FRAME.replace(b"0000000b", b"+000000b"),
      FRAME.replace(b"0000000b", b""),
      FRAME.replace(b" selection", b"  selection"),
      FRAME.replace(b" selection", b"\tselection"),
      FRAME.replace(b" selection", b"\nselection"),
      FRAME.replace(b"\r\n", b" \r\n"),
      FRAME.replace(b"0000000b", b"0000000\xff"),
      FRAME.decode("ascii").replace("0000000b", "0000000é"),
    ]
    for frame in bad_frames:
      with self.subTest(frame=frame), self.assertRaises(ValueError):
        _ = read_stats.parse_snapshot(frame)


class UartTests(unittest.TestCase):
  @override
  def setUp(self):
    self.master, self.slave = pty.openpty()
    self.port = os.ttyname(self.slave)
    self.original = termios.tcgetattr(self.slave)
    self.addCleanup(os.close, self.slave)
    self.addCleanup(self.close_master)

  def close_master(self):
    if self.master is not None:
      os.close(self.master)
      self.master = None

  def start_board(self, chunks=(), *, disconnect=False, delay=0.005):
    errors = []
    master = self.master
    assert master is not None

    def respond():
      try:
        ready, _, _ = select.select([master], [], [], 5)
        if not ready:
          raise AssertionError("reader did not send a request")
        self.assertEqual(os.read(master, 1), b"?")
        for chunk in chunks:
          _ = os.write(master, chunk)
          time.sleep(delay)
        if disconnect:
          self.close_master()
      except Exception as exc:  # noqa: BLE001 - forward worker failures to the test thread
        errors.append(exc)

    thread = threading.Thread(target=respond, daemon=True)
    thread.start()

    def join():
      thread.join(6)
      self.assertFalse(thread.is_alive(), "board responder did not finish")
      if errors:
        raise errors[0]

    self.addCleanup(join)
    return thread

  def run_cli(self, *args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
      result = read_stats.main([self.port, *args])
    return result, stdout.getvalue(), stderr.getvalue()

  def assert_restored(self):
    self.assertEqual(termios.tcgetattr(self.slave), self.original)

  def test_raw_configuration_fragmented_frame_and_restore(self):
    _ = self.start_board([FRAME[:1], FRAME[1:17], FRAME[17:-1], FRAME[-1:]])
    with read_stats.open_uart(self.port) as fd:
      attrs = termios.tcgetattr(fd)
      self.assertEqual(attrs[0], 0)
      self.assertEqual(attrs[1], 0)
      self.assertEqual(attrs[3], 0)
      self.assertEqual(attrs[2] & termios.CSIZE, termios.CS8)
      disabled = termios.PARENB | termios.CSTOPB | getattr(termios, "CRTSCTS", 0)
      self.assertEqual(attrs[2] & disabled, 0)
      self.assertEqual(attrs[4:6], [termios.B115200, termios.B115200])
      self.assertEqual(attrs[6][termios.VMIN], 0)
      self.assertEqual(attrs[6][termios.VTIME], 0)
      self.assertEqual(read_stats.read_snapshot(fd, 1), EXPECTED)
    self.assert_restored()

  def test_configuration_and_flush_settle_before_first_query(self):
    events = []
    real_setattr = termios.tcsetattr
    real_flush = termios.tcflush
    real_write = os.write
    master = self.master
    assert master is not None

    def configure(fd, when, attrs):
      events.append("restore" if events else "configure")
      self.assertEqual(when, termios.TCSANOW)
      real_setattr(fd, when, attrs)

    def flush(fd, queue):
      self.assertEqual(events, ["configure"])
      self.assertEqual(queue, termios.TCIOFLUSH)
      real_flush(fd, queue)
      events.append("flush")

    def settle(seconds):
      self.assertEqual(events, ["configure", "flush"])
      self.assertEqual(seconds, read_stats.UART_SETTLE_SECONDS)
      self.assertEqual(seconds, 0.05)
      attrs = termios.tcgetattr(self.slave)
      self.assertEqual(attrs[0], 0)
      self.assertEqual(attrs[1], 0)
      self.assertEqual(attrs[3], 0)
      self.assertEqual(attrs[4:6], [termios.B115200, termios.B115200])
      events.append("settle")

    def query(fd, data):
      self.assertEqual(events, ["configure", "flush", "settle"])
      self.assertEqual(data, b"?")
      count = real_write(fd, data)
      self.assertEqual(os.read(master, 1), b"?")
      events.append("query")
      _ = real_write(master, FRAME)
      return count

    with (
      patch.object(termios, "tcsetattr", side_effect=configure),
      patch.object(termios, "tcflush", side_effect=flush),
      patch("ivga.slop.read_stats.time.sleep", side_effect=settle),
      patch.object(os, "write", side_effect=query),
      read_stats.open_uart(self.port) as fd,
    ):
      self.assertEqual(events, ["configure", "flush", "settle"])
      self.assertEqual(read_stats.read_snapshot(fd, 1), EXPECTED)
    self.assertEqual(events, ["configure", "flush", "settle", "query", "restore"])
    self.assert_restored()

  def test_cli_queries_across_repeated_close_and_reopen(self):
    for count in range(3):
      with self.subTest(query=count):
        frame = FRAME.replace(b"alloc_allocs=0000002a", f"alloc_allocs={count:08x}".encode())
        thread = self.start_board([frame])
        code, stdout, stderr = self.run_cli("--timeout", "1")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        output = json.loads(stdout)
        expected = {**EXPECTED, "alloc_allocs": count}
        self.assertEqual({key: output[key] for key in EXPECTED}, expected)
        thread.join(1)
        self.assertFalse(thread.is_alive(), "board responder did not finish")
        self.assert_restored()

  def test_timeout_and_partial_frame_restore(self):
    for chunks in ([], [b"ivga1 status="]):
      with self.subTest(chunks=chunks):
        thread = self.start_board(chunks)
        start = time.monotonic()
        with (
          self.assertRaisesRegex(TimeoutError, "timed out"),
          read_stats.open_uart(self.port) as fd,
        ):
          _ = read_stats.read_snapshot(fd, 0.1)
        self.assertLess(time.monotonic() - start, 1)
        thread.join(1)
        self.assert_restored()

  def test_overlong_frame_is_bounded_and_restored(self):
    _ = self.start_board([b"x" * (read_stats.MAX_FRAME_BYTES + 20)])
    with (
      self.assertRaisesRegex(ValueError, "exceeds"),
      read_stats.open_uart(self.port) as fd,
    ):
      _ = read_stats.read_snapshot(fd, 1)
    self.assert_restored()

  def test_advisory_lock_prevents_second_reader(self):
    with (
      read_stats.open_uart(self.port),
      self.assertRaisesRegex(OSError, "locked"),
      read_stats.open_uart(self.port),
    ):
      self.fail("second reader acquired the lock")
    self.assert_restored()
    # Closing the first reader releases its lock.
    fcntl.flock(self.slave, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(self.slave, fcntl.LOCK_UN)

  def test_cli_numeric_json_and_flags(self):
    _ = self.start_board([FRAME[:30], FRAME[30:]])
    code, stdout, stderr = self.run_cli("--timeout", "1")
    self.assertEqual(code, 0, stderr)
    self.assertEqual(stderr, "")
    output = json.loads(stdout)
    self.assertEqual({key: output[key] for key in EXPECTED}, EXPECTED)
    self.assertEqual(output["status_flags"], {
      "busy": True, "done": True, "halted": False, "timed_out": True,
    })
    self.assertEqual(output["selection_bits"], [True, False, True, False])
    self.assertIn('\n  "status": 11,', stdout)
    self.assert_restored()

  def test_cli_timeout_message(self):
    code, stdout, stderr = self.run_cli("--timeout", "0.05")
    self.assertEqual(code, 1)
    self.assertEqual(stdout, "")
    self.assertIn("timed out", stderr)
    self.assertIn("--timeout", stderr)
    self.assert_restored()

  def test_cli_malformed_frame_message(self):
    _ = self.start_board([FRAME.replace(b"ivga1", b"ivga2")])
    code, _, stderr = self.run_cli()
    self.assertEqual(code, 1)
    self.assertIn("invalid statistics frame", stderr)
    self.assert_restored()

  def test_cli_disconnect_message(self):
    _ = self.start_board(disconnect=True)
    code, _, stderr = self.run_cli()
    self.assertEqual(code, 1)
    self.assertIn("UART I/O failed", stderr)
    self.assertIn("connection", stderr)

  def test_ctrl_c_restores_tty(self):
    with patch.object(read_stats, "read_snapshot", side_effect=KeyboardInterrupt):
      code, _, stderr = self.run_cli()
    self.assertEqual(code, 130)
    self.assertIn("Stopped", stderr)
    self.assert_restored()

  def test_watch_settles_then_reads_multiple_frames_before_interrupt(self):
    interval = 0.01
    polls = 0

    def sleep(seconds):
      nonlocal polls
      if seconds == read_stats.UART_SETTLE_SECONDS:
        self.assertEqual(polls, 0)
        reader.assert_not_called()
        return
      self.assertEqual(seconds, interval)
      polls += 1
      self.assertEqual(reader.call_count, polls)
      if polls == 3:
        raise KeyboardInterrupt

    # Patch only the reader's time reference, not the PTY responder's sleeps.
    with (
      patch.object(read_stats, "read_snapshot", return_value=EXPECTED) as reader,
      patch.object(read_stats, "time", wraps=time) as clock,
    ):
      clock.sleep.side_effect = sleep
      code, stdout, stderr = self.run_cli("--watch", "--interval", str(interval))
      self.assertEqual(clock.sleep.call_args_list[0].args, (read_stats.UART_SETTLE_SECONDS,))
      self.assertEqual(clock.sleep.call_count, 4)
      self.assertEqual(reader.call_count, 3)
    self.assertEqual(code, 130)
    self.assertIn("Stopped", stderr)
    decoder = json.JSONDecoder()
    for _ in range(3):
      output, end = decoder.raw_decode(stdout.lstrip())
      self.assertEqual({key: output[key] for key in EXPECTED}, EXPECTED)
      stdout = stdout.lstrip()[end:]
    self.assertEqual(stdout.strip(), "")
    self.assert_restored()

  def test_watch_flushes_before_sleep_and_ctrl_c_restores(self):
    _ = self.start_board([FRAME])
    process = subprocess.Popen(
      [sys.executable, "-m", "ivga.slop.read_stats", self.port,
       "--watch", "--interval", "10", "--timeout", "1"],
      cwd=Path(__file__).resolve().parents[2],
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
    )

    def cleanup():
      if process.poll() is None:
        process.kill()
      _ = process.communicate(timeout=3)

    self.addCleanup(cleanup)
    assert process.stdout is not None
    data = b""
    deadline = time.monotonic() + 5
    while True:
      remaining = deadline - time.monotonic()
      self.assertGreater(remaining, 0, "watch output was not flushed")
      ready, _, _ = select.select([process.stdout], [], [], remaining)
      self.assertTrue(ready, "watch output was not flushed")
      chunk = os.read(process.stdout.fileno(), 4096)
      self.assertTrue(chunk, "watch exited without a snapshot")
      data += chunk
      try:
        output = json.loads(data)
        break
      except json.JSONDecodeError:
        continue
    self.assertEqual(output["alloc_allocs"], 42)
    process.send_signal(signal.SIGINT)
    _, stderr = process.communicate(timeout=3)
    self.assertEqual(process.returncode, 130, stderr.decode())
    self.assertIn(b"Stopped", stderr)
    self.assert_restored()

  def test_invalid_arguments_do_not_touch_tty(self):
    for option in ("--timeout", "--interval"):
      for value in ("0", "-1", "nan", "inf", "abc"):
        with self.subTest(option=option, value=value):
          with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            _ = read_stats.main([self.port, option, value])
          self.assertEqual(exc.exception.code, 2)
          self.assert_restored()

  def test_missing_port_message(self):
    with patch.object(os, "open", side_effect=FileNotFoundError("not found")):
      code, _, stderr = self.run_cli()
    self.assertEqual(code, 1)
    self.assertIn(self.port, stderr)
    self.assertIn("permissions", stderr)


if __name__ == "__main__":
  _ = unittest.main()
