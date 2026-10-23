"""End-to-end harness tests: run ``python -m ivga.slop.test_top_slop``.

All readout checks decode the actual 8N1 TX pin; no serial device or trace
files are used. Small divisors/debounce/watchdog settings keep simulation fast.
"""

import unittest

from amaranth import Const, Module, Signal
from amaranth.sim import Simulator

from ivga import PORT_EMPTY, Port, Tag
from ivga.mem.alloc import ADDR_EMPTY
from ivga.slop.read_stats import FIELDS as READER_FIELDS
from ivga.slop.read_stats import MAX_FRAME_BYTES, parse_snapshot
from ivga.slop.stats_uart import FIELDS, StatsUART
from ivga.slop.top_slop import Processor

PERIOD = 1e-8
DIVISOR = 8
BIT_PERIOD = DIVISOR * PERIOD
FRAME_CYCLES = MAX_FRAME_BYTES * (10 * DIVISOR + 3) + 128
COUNTERS = FIELDS[2:]


def processor():
  return Processor(debounce_cycles=2, timeout_cycles=128, uart_divisor=DIVISOR)


def counters(ctx, dut):
  return tuple(ctx.get(signal) for signal in (
    dut.stat_allocs, dut.stat_frees, dut.stat_peak,
    dut.stat_erase, dut.stat_annihilate,
  ))


def expected_counters(selection):
  reserved = (selection >> 2) * 4 + 4
  erasers = selection & 1
  return (reserved, 4 * erasers, reserved, 2 * erasers, 1)


def expected_heap(selection, depth):
  words = [PORT_EMPTY.as_value().value] * depth
  base = (selection >> 2) * 4
  if selection & 1:
    # Eraser linking frees B0, A0, B1, A1, storing the previous free head.
    words[base:base + 4] = [base + 2, base + 3, ADDR_EMPTY.value, base]
  else:
    for offset in range(2):
      words[base + offset] = Const(
        {"tag": Tag.WIRE, "payload": base + offset + 2, "label": 0}, Port,
      ).as_value().value
  return words


class SerialMonitor:
  def __init__(self, test, tx):
    self.test = test
    self.tx = tx
    self.frames = []
    self.lines = []
    self.partial = bytearray()
    self.byte_count = 0

  async def run(self, ctx):
    while True:
      await ctx.negedge(self.tx)
      await ctx.delay(BIT_PERIOD / 2)
      self.test.assertEqual(ctx.get(self.tx), 0, "invalid TX start bit")
      value = 0
      for bit in range(8):
        await ctx.delay(BIT_PERIOD)
        value |= ctx.get(self.tx) << bit
      await ctx.delay(BIT_PERIOD)
      self.test.assertEqual(ctx.get(self.tx), 1, "invalid TX stop bit")
      self.byte_count += 1
      self.partial.append(value)
      self.test.assertLessEqual(len(self.partial), MAX_FRAME_BYTES)
      if value == ord("\n"):
        line = bytes(self.partial)
        self.lines.append(line)
        self.frames.append(parse_snapshot(line))
        self.partial.clear()

  async def wait_frame(self, ctx, index):
    for _ in range((FRAME_CYCLES + 31) // 32):
      if len(self.frames) > index:
        # The decoder samples at the stop-bit center; let the byte finish.
        await ctx.tick().repeat(DIVISOR)
        return self.frames[index]
      await ctx.tick().repeat(32)
    self.test.fail(f"no complete UART frame {index}; partial={bytes(self.partial)!r}")


async def send_byte(ctx, rx, value):
  # Keep the host transitions away from rising clock edges.
  await ctx.delay(PERIOD / 3)
  for bit in [0] + [(value >> index) & 1 for index in range(8)] + [1]:
    ctx.set(rx, bit)
    await ctx.delay(BIT_PERIOD)
  await ctx.tick().repeat(4)


async def press(ctx, button):
  ctx.set(button, 1)
  await ctx.tick().repeat(8)
  ctx.set(button, 0)
  await ctx.tick().repeat(8)


class TopSlopTests(unittest.TestCase):
  def simulate(self, dut, tx, bench):
    monitor = SerialMonitor(self, tx)
    sim = Simulator(dut)
    sim.add_clock(PERIOD)
    sim.add_testbench(monitor.run, background=True)

    async def run(ctx):
      await bench(ctx, monitor)

    sim.add_testbench(run)
    sim.run()

  async def start(self, ctx, dut, selection):
    ctx.set(dut.switches, selection)
    await ctx.tick().repeat(4)
    await press(ctx, dut.start_button)
    self.assertEqual(ctx.get(dut.selection), selection)
    self.assertEqual(ctx.get(dut.busy), 1)
    self.assertEqual(ctx.get(dut.done), 0)
    self.assertEqual(ctx.get(dut.halted), 0)
    self.assertEqual(ctx.get(dut.timed_out), 0)
    self.assertEqual(counters(ctx, dut), (0, 0, 0, 0, 0))

  async def wait_terminal(self, ctx, dut):
    for _ in range(100):
      if not ctx.get(dut.busy):
        return
      await ctx.tick().repeat(16)
    self.fail("core did not finish or time out within 1600 cycles")

  async def query(self, ctx, dut, monitor):
    index = len(monitor.frames)
    await send_byte(ctx, dut.uart_rx, ord("?"))
    return await monitor.wait_frame(ctx, index)

  def assert_snapshot(self, snapshot, status, selection, counts):
    self.assertEqual(snapshot, dict(zip(
      FIELDS, (status, selection, *counts), strict=True,
    )))

  def test_protocol_fields_match_reader(self):
    self.assertEqual(FIELDS, READER_FIELDS)
    self.assertEqual(COUNTERS, (
      "alloc_allocs", "alloc_frees", "alloc_peak", "link_erase",
      "interact_annihilate",
    ))

  def test_both_presets_all_banks_restart_and_reset(self):
    dut = processor()

    async def bench(ctx, monitor):
      for bank in range(4):
        for erasers in range(2):
          selection = (bank << 2) | erasers
          with self.subTest(bank=bank, erasers=erasers):
            # A new Start after Done must clear every previous counter.
            await self.start(ctx, dut, selection)
            # Switch changes during a run must not change the captured preset.
            ctx.set(dut.switches, selection ^ 0xF)
            await self.wait_terminal(ctx, dut)
            self.assertEqual(ctx.get(dut.done), 1)
            self.assertEqual(ctx.get(dut.halted), 0)
            self.assertEqual(ctx.get(dut.timed_out), 0)
            expected = expected_counters(selection)
            self.assertEqual(counters(ctx, dut), expected)
            words = expected_heap(selection, dut.heap.depth)
            self.assertEqual(
              [ctx.get(dut.heap.data[address]) for address in range(dut.heap.depth)],
              words,
            )
            snapshot = await self.query(ctx, dut, monitor)
            self.assert_snapshot(snapshot, 0b0010, selection, expected)
            self.assertEqual(counters(ctx, dut), expected)

      selection_before_reset = ctx.get(dut.selection)
      await press(ctx, dut.reset_button)
      self.assertEqual(counters(ctx, dut), (0, 0, 0, 0, 0))
      self.assertEqual(ctx.get(dut.done), 0)
      self.assertEqual(ctx.get(dut.busy), 0)
      self.assertEqual(ctx.get(dut.halted), 0)
      self.assertEqual(ctx.get(dut.timed_out), 0)
      snapshot = await self.query(ctx, dut, monitor)
      self.assert_snapshot(snapshot, 0, selection_before_reset, (0, 0, 0, 0, 0))
      self.assertEqual(len(monitor.frames), 9)
      self.assertFalse(monitor.partial)

    self.simulate(dut, dut.uart_tx, bench)

  def test_busy_halt_button_snapshot_and_timeout(self):
    dut = processor()

    async def bench(ctx, monitor):
      await self.start(ctx, dut, 2)
      await send_byte(ctx, dut.uart_rx, ord("?"))
      self.assertEqual(ctx.get(dut.busy), 1)
      self.assertEqual(counters(ctx, dut), (0, 0, 0, 0, 0))
      # Halt during the response: UART must finish the pre-halt busy snapshot.
      await press(ctx, dut.halt_button)
      self.assertEqual(ctx.get(dut.halted), 1)
      self.assertEqual(ctx.get(dut.busy), 0)
      self.assertEqual(ctx.get(dut.done), 0)
      self.assertEqual(ctx.get(dut.timed_out), 0)
      snapshot = await monitor.wait_frame(ctx, 0)
      self.assert_snapshot(snapshot, 0b0001, 2, (0, 0, 0, 0, 0))

      # BTN3 is an independent snapshot request, including while halted.
      await press(ctx, dut.read_button)
      snapshot = await monitor.wait_frame(ctx, 1)
      self.assert_snapshot(snapshot, 0b0100, 2, (0, 0, 0, 0, 0))
      await press(ctx, dut.start_button)
      self.assertEqual(ctx.get(dut.halted), 1)
      self.assertEqual(ctx.get(dut.busy), 0)
      self.assertEqual(counters(ctx, dut), (0, 0, 0, 0, 0))

      await press(ctx, dut.reset_button)
      self.assertEqual(ctx.get(dut.halted), 0)
      self.assertEqual(ctx.get(dut.timed_out), 0)
      self.assertEqual(counters(ctx, dut), (0, 0, 0, 0, 0))
      # Mismatched labels exercise the real watchdog, not a forced fault input.
      await self.start(ctx, dut, 14)
      await self.wait_terminal(ctx, dut)
      self.assertEqual(ctx.get(dut.halted), 1)
      self.assertEqual(ctx.get(dut.timed_out), 1)
      self.assertEqual(ctx.get(dut.done), 0)
      expected = (16, 0, 16, 0, 0)
      self.assertEqual(counters(ctx, dut), expected)
      snapshot = await self.query(ctx, dut, monitor)
      self.assert_snapshot(snapshot, 0b1100, 14, expected)
      self.assertEqual(counters(ctx, dut), expected)
      self.assertEqual(len(monitor.frames), 3)

    self.simulate(dut, dut.uart_tx, bench)

  def test_stats_atomic_free_running_values_and_ignored_requests(self):
    m = Module()
    initial = (0xFFFFFFF0, 0x89ABCDEF, 0x12345678, 0xDEADBEEF,
               0x0BADCAFE, 0x76543210, 0xFEDCBA98)
    values = tuple(Signal(32, init=value) for value in initial)
    for index, value in enumerate(values):
      m.d.sync += value.eq(value + index + 1)
    m.submodules.stats = stats = StatsUART(values, divisor=DIVISOR)

    async def bench(ctx, monitor):
      await send_byte(ctx, stats.rx, ord("x"))
      await ctx.tick().repeat(30 * DIVISOR)
      self.assertEqual(monitor.byte_count, 0, "unknown command produced output")
      self.assertEqual(ctx.get(stats.sending), 0)
      self.assertEqual(ctx.get(stats.tx), 1)

      async def request():
        expected = tuple(ctx.get(value) for value in values)
        ctx.set(stats.request, 1)
        await ctx.tick()
        ctx.set(stats.request, 0)
        return dict(zip(FIELDS, expected, strict=True))

      expected = await request()
      self.assertEqual(ctx.get(stats.sending), 1)
      await ctx.tick().repeat(20 * DIVISOR)
      self.assertTrue(all(
        ctx.get(value) != expected[name]
        for name, value in zip(FIELDS, values, strict=True)
      ))
      # Neither another button pulse nor another '?' may queue a response.
      _ = await request()
      await send_byte(ctx, stats.rx, ord("?"))
      snapshot = await monitor.wait_frame(ctx, 0)
      self.assertEqual(snapshot, expected)
      await ctx.tick().repeat(30 * DIVISOR)
      self.assertEqual(len(monitor.frames), 1)
      self.assertEqual(monitor.byte_count, MAX_FRAME_BYTES)
      self.assertFalse(monitor.partial)
      self.assertEqual(ctx.get(stats.tx), 1)

      # Once idle, a fresh request must capture all the new values together.
      expected = await request()
      snapshot = await monitor.wait_frame(ctx, 1)
      self.assertEqual(snapshot, expected)
      self.assertNotEqual(monitor.frames[0], monitor.frames[1])
      self.assertEqual(len(monitor.frames), 2)

    self.simulate(m, stats.tx, bench)


if __name__ == "__main__":
  _ = unittest.main()
