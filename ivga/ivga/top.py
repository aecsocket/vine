from amaranth import Cat, Module, Signal
from amaranth.lib import io, wiring
from amaranth.lib.cdc import FFSynchronizer

from ivga.runtime.runtime import Runtime
from ivga.util.stats_uart import StatsUART, runtime_values


class Processor(wiring.Component):
  """BTN0 starts; LED0 latches completion, LED1 signals failure, UART reports registers."""

  def __init__(self, *, active_depth: int = 128, reserved_pages: int = 0,
               debounce_cycles: int = 500_000, uart_divisor: int = 868):
    assert debounce_cycles > 0
    self.debounce_cycles = debounce_cycles
    self.runtime = Runtime(active_depth=active_depth, reserved_pages=reserved_pages)
    self.done_led = Signal()
    self.stats = StatsUART(runtime_values(self.runtime, done=self.done_led | self.runtime.done),
                           divisor=uart_divisor)
    self.uart_rx = self.stats.rx
    self.uart_tx = self.stats.tx
    super().__init__(self.runtime.signature)

  def elaborate(self, platform):
    m = Module()
    m.submodules.runtime = self.runtime
    m.submodules.stats = self.stats
    wiring.connect(m, wiring.flipped(self), self.runtime)

    faults = Cat(*(value for name, value in zip(self.stats.fields, self.stats.values, strict=True)
                   if name.startswith("fault_")), self.runtime.halted)
    previous_faults = Signal(len(faults))
    m.d.sync += previous_faults.eq(faults)
    m.d.comb += self.stats.request.eq(self.runtime.done | (faults != previous_faults))

    with m.If(self.runtime.busy):
      m.d.sync += self.done_led.eq(False)
    with m.Elif(self.runtime.done):
      m.d.sync += self.done_led.eq(True)

    if platform is not None:
      button = m.submodules.button_0 = io.Buffer("i", platform.request("button", 0, dir="-"))
      synchronized = Signal()
      stable = Signal()
      count = Signal(range(self.debounce_cycles))
      m.submodules.button_sync = FFSynchronizer(button.i, synchronized)
      with m.If(synchronized == stable):
        m.d.sync += count.eq(0)
      with m.Elif(count == self.debounce_cycles - 1):
        m.d.sync += [stable.eq(synchronized), count.eq(0)]
      with m.Else():
        m.d.sync += count.eq(count + 1)
      m.d.comb += self.start.eq(stable)

      for index, signal in enumerate((self.done_led, self.runtime.failed)):
        led = io.Buffer("o", platform.request("led", index, dir="-"))
        m.submodules[f"led_{index}"] = led
        m.d.comb += led.o.eq(signal)

      pins = platform.request("uart", 0, dir={"rx": "-", "tx": "-"})
      rx = m.submodules.uart_rx = io.Buffer("i", pins.rx)
      tx = m.submodules.uart_tx = io.Buffer("o", pins.tx)
      m.d.comb += [self.uart_rx.eq(rx.i), tx.o.eq(self.uart_tx)]

    return m
