"""Minimal synchronous 8N1 UART, with one bit per ``divisor`` clock cycles.

At 100 MHz, divisor=868 gives approximately 115200 baud. TX accepts data on
an edge with tx_valid & tx_ready; keep data stable until that handshake. RX
has no backpressure: consume rx_data whenever the one-cycle rx_valid is high.
Both directions use the sync domain and idle high.
"""

from amaranth import Cat, Const, Elaboratable, Module, Signal
from amaranth.lib.cdc import FFSynchronizer


class UART(Elaboratable):
  def __init__(self, divisor: int):
    # Runtime validation is still needed for callers without type checking.
    if not isinstance(divisor, int) or divisor < 4:  # pyright: ignore[reportUnnecessaryIsInstance]
      raise ValueError("divisor must be an integer >= 4")
    self.divisor = divisor

    self.rx = Signal(init=1)
    self.tx = Signal(init=1)
    self.rx_data = Signal(8)
    self.rx_valid = Signal()
    self.tx_data = Signal(8)
    self.tx_valid = Signal()
    self.tx_ready = Signal()

  def elaborate(self, platform):
    m = Module()
    divisor = self.divisor

    tx_busy = Signal()
    tx_timer = Signal(range(divisor))
    tx_bit = Signal(range(10))
    tx_shift = Signal(9)
    m.d.comb += self.tx_ready.eq(~tx_busy)

    with m.If(~tx_busy), m.If(self.tx_valid & self.tx_ready):
      m.d.sync += [
        tx_busy.eq(1),
        tx_timer.eq(divisor - 1),
        tx_bit.eq(0),
        tx_shift.eq(Cat(self.tx_data, Const(1, 1))),
        self.tx.eq(0),
      ]
    with m.Elif(tx_timer != 0):
      m.d.sync += tx_timer.eq(tx_timer - 1)
    with m.Else():
      with m.If(tx_bit == 9):
        m.d.sync += tx_busy.eq(0)
      with m.Else():
        m.d.sync += [
          tx_timer.eq(divisor - 1),
          tx_bit.eq(tx_bit + 1),
          self.tx.eq(tx_shift[0]),
          tx_shift.eq(tx_shift >> 1),
        ]

    rx_sync = Signal(init=1)
    m.submodules.rx_sync = FFSynchronizer(self.rx, rx_sync, init=1, stages=2)
    rx_timer = Signal(range(divisor))
    rx_bit = Signal(3)
    rx_shift = Signal(8)
    m.d.sync += self.rx_valid.eq(0)

    with m.FSM(name="rx", init="IDLE"):
      with m.State("IDLE"), m.If(~rx_sync):
        m.d.sync += rx_timer.eq(divisor // 2 - 1)
        m.next = "START"

      with m.State("START"):
        with m.If(rx_timer != 0):
          m.d.sync += rx_timer.eq(rx_timer - 1)
        with m.Elif(rx_sync):
          m.next = "IDLE"
        with m.Else():
          m.d.sync += [rx_timer.eq(divisor - 1), rx_bit.eq(0)]
          m.next = "DATA"

      with m.State("DATA"):
        with m.If(rx_timer != 0):
          m.d.sync += rx_timer.eq(rx_timer - 1)
        with m.Else():
          m.d.sync += [
            rx_shift.bit_select(rx_bit, 1).eq(rx_sync),
            rx_timer.eq(divisor - 1),
          ]
          with m.If(rx_bit == 7):
            m.next = "STOP"
          with m.Else():
            m.d.sync += rx_bit.eq(rx_bit + 1)

      with m.State("STOP"):
        with m.If(rx_timer != 0):
          m.d.sync += rx_timer.eq(rx_timer - 1)
        with m.Elif(rx_sync):
          m.d.sync += [self.rx_data.eq(rx_shift), self.rx_valid.eq(1)]
          m.next = "IDLE"
        with m.Else():
          # A break or bad stop must return high before another start.
          m.next = "WAIT_HIGH"

      with m.State("WAIT_HIGH"), m.If(rx_sync):
        m.next = "IDLE"

    return m
