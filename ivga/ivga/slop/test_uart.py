"""UART simulation tests: run ``python -m ivga.slop.test_uart``."""

import unittest

from amaranth.sim import Simulator

from ivga.slop.uart import UART

CLOCK_PERIOD = 1e-8
DIVISORS = (4, 5, 868)


def frame_bits(value, stop=1):
  return [0] + [(value >> bit) & 1 for bit in range(8)] + [stop]


def rx_frame(value, divisor, stop=1):
  return [(bit, divisor) for bit in frame_bits(value, stop)]


class UARTTests(unittest.TestCase):
  def simulate(self, dut, testbench):
    sim = Simulator(dut)
    sim.add_clock(CLOCK_PERIOD)
    sim.add_testbench(testbench)
    sim.run()

  def test_tx_bit_order_and_timing(self):
    for divisor in DIVISORS:
      with self.subTest(divisor=divisor):
        dut = UART(divisor)

        async def bench(ctx, dut=dut, divisor=divisor):
          for _ in range(3):
            self.assertEqual(ctx.get(dut.tx), 1)
            self.assertEqual(ctx.get(dut.tx_ready), 1)
            self.assertEqual(ctx.get(dut.rx_valid), 0)
            await ctx.tick()

          for value in (0x00, 0xFF, 0xA5, 0x81):
            ctx.set(dut.tx_data, value)
            ctx.set(dut.tx_valid, 1)
            await ctx.tick()
            ctx.set(dut.tx_valid, 0)
            ctx.set(dut.tx_data, value ^ 0xFF)
            for bit in frame_bits(value):
              for _ in range(divisor):
                self.assertEqual(ctx.get(dut.tx), bit)
                self.assertEqual(ctx.get(dut.tx_ready), 0)
                await ctx.tick()
            self.assertEqual(ctx.get(dut.tx), 1)
            self.assertEqual(ctx.get(dut.tx_ready), 1)
            for _ in range(2):
              await ctx.tick()
              self.assertEqual(ctx.get(dut.tx), 1)
              self.assertEqual(ctx.get(dut.tx_ready), 1)

        self.simulate(dut, bench)

  def test_tx_ready_and_back_to_back_handshakes(self):
    divisor = 5
    dut = UART(divisor)

    async def bench(ctx):
      ctx.set(dut.tx_valid, 1)
      for value in (0x96, 0x3C, 0x81):
        self.assertEqual(ctx.get(dut.tx_ready), 1)
        ctx.set(dut.tx_data, value)
        await ctx.tick()
        # Valid stays asserted, but busy-cycle data must not be accepted.
        ctx.set(dut.tx_data, value ^ 0xFF)
        for bit in frame_bits(value):
          for _ in range(divisor):
            self.assertEqual(ctx.get(dut.tx_ready), 0)
            self.assertEqual(ctx.get(dut.tx), bit)
            await ctx.tick()
        self.assertEqual(ctx.get(dut.tx_ready), 1)

      ctx.set(dut.tx_valid, 0)
      for _ in range(12 * divisor):
        await ctx.tick()
        self.assertEqual(ctx.get(dut.tx), 1)
        self.assertEqual(ctx.get(dut.tx_ready), 1)

    self.simulate(dut, bench)

  def check_rx(self, divisor, waveform, expected):
    dut = UART(divisor)
    received = []

    async def monitor(ctx):
      previous_valid = 0
      previous_data = 0
      while True:
        await ctx.tick()
        valid = ctx.get(dut.rx_valid)
        data = ctx.get(dut.rx_data)
        if valid:
          self.assertFalse(previous_valid, "rx_valid must be a one-cycle pulse")
          received.append(data)
        else:
          self.assertEqual(data, previous_data)
        previous_valid = valid
        previous_data = data

    async def bench(ctx):
      await ctx.tick().repeat(6)
      # Drive away from clock edges to exercise the asynchronous RX input.
      await ctx.delay(0.37 * CLOCK_PERIOD)
      for level, cycles in waveform:
        ctx.set(dut.rx, level)
        await ctx.delay(cycles * CLOCK_PERIOD)
      ctx.set(dut.rx, 1)
      await ctx.tick().repeat(12 * divisor + 6)
      self.assertEqual(received, expected)
      self.assertEqual(ctx.get(dut.rx_valid), 0)

    sim = Simulator(dut)
    sim.add_clock(CLOCK_PERIOD)
    sim.add_testbench(monitor, background=True)
    sim.add_testbench(bench)
    sim.run()

  def test_rx_valid_frames(self):
    values = [0x00, 0xFF, 0xA5, 0x81]
    for divisor in DIVISORS:
      with self.subTest(divisor=divisor):
        waveform = []
        for value in values:
          waveform += rx_frame(value, divisor) + [(1, 3 * divisor)]
        self.check_rx(divisor, waveform, values)

  def test_rx_back_to_back_frames(self):
    values = [0x00, 0xFF, 0x96, 0x01, 0x80]
    for divisor in DIVISORS:
      with self.subTest(divisor=divisor):
        waveform = []
        for value in values:
          waveform += rx_frame(value, divisor)
        self.check_rx(divisor, waveform, values)

  def test_rx_false_start(self):
    for divisor in DIVISORS:
      with self.subTest(divisor=divisor):
        waveform = [
          (0, 1), (1, 12 * divisor),
          (0, divisor // 2 - 1), (1, 12 * divisor),
        ] + rx_frame(0x69, divisor)
        self.check_rx(divisor, waveform, [0x69])

  def test_rx_bad_stop_and_break_recovery(self):
    for divisor in DIVISORS:
      with self.subTest(divisor=divisor):
        waveform = (
          rx_frame(0xA5, divisor, stop=0)
          + [(1, 12 * divisor)]
          + rx_frame(0x00, divisor, stop=0)
          + [(0, 20 * divisor), (1, 2 * divisor)]
          + rx_frame(0x3C, divisor)
        )
        self.check_rx(divisor, waveform, [0x3C])


if __name__ == "__main__":
  _ = unittest.main()
