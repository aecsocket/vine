"""Coherent ivga1 counter snapshots over a 115200-baud, 8N1 UART.

Send '?' or pulse request to capture every field on the same clock edge.
The response contains eight lowercase hex digits per unsigned 32-bit field.
Requests while a response is queued are ignored; wait for CRLF before polling.
"""

from amaranth import Array, Const, Elaboratable, Module, Mux, Signal, Value

from ivga.slop.uart import UART

FIELDS = (
  "status", "selection", "alloc_allocs", "alloc_frees", "alloc_peak",
  "link_erase", "interact_annihilate",
)


class StatsUART(Elaboratable):
  def __init__(self, values, divisor=868):
    self.values = tuple(Value.cast(value) for value in values)
    if len(self.values) != len(FIELDS) or any(len(value) > 32 for value in self.values):
      raise ValueError("ivga1 requires seven fields of at most 32 bits")
    self.uart = UART(divisor)
    self.rx = self.uart.rx
    self.tx = self.uart.tx
    self.request = Signal()
    self.sending = Signal()
    self.snapshot = tuple(Signal(32, name=f"snapshot_{name}") for name in FIELDS)

  def elaborate(self, platform):
    m = Module()
    m.submodules.uart = uart = self.uart
    characters: list[Value] = [Const(ord(char), 8) for char in "ivga1 "]
    for index, (name, value) in enumerate(zip(FIELDS, self.snapshot, strict=True)):
      if index:
        characters.append(Const(ord(" "), 8))
      characters.extend(Const(ord(char), 8) for char in name + "=")
      for nibble_index in reversed(range(8)):
        nibble = Value.cast(value.word_select(nibble_index, 4))
        characters.append(Mux(nibble < 10, nibble + ord("0"), nibble + ord("a") - 10))
    characters.extend((Const(ord("\r"), 8), Const(ord("\n"), 8)))
    position = Signal(range(len(characters)))
    m.d.comb += [
      uart.tx_valid.eq(self.sending),
      uart.tx_data.eq(Array(characters)[position]),
    ]

    request = self.request | (uart.rx_valid & (uart.rx_data == ord("?")))
    with m.If(~self.sending & request):
      m.d.sync += [self.sending.eq(True), position.eq(0)]
      for snapshot, value in zip(self.snapshot, self.values, strict=True):
        m.d.sync += snapshot.eq(value)
    with m.Elif(self.sending & uart.tx_ready):
      with m.If(position == len(characters) - 1):
        m.d.sync += self.sending.eq(False)
      with m.Else():
        m.d.sync += position.eq(position + 1)
    return m
