"""Atomic named-register snapshots over a 115200-baud, 8N1 UART.

Frames are ASCII: ivga2 name=00000000 other_name=ffffffff\r\n.
All values are captured on one edge, not read while the frame is transmitting.
Send '?' or pulse request for a snapshot. Busy requests coalesce into one pending
snapshot of the latest registers, so a completion during transmission is retained.
"""

import re
from collections.abc import Mapping

from amaranth import Array, Const, Elaboratable, Module, Mux, Signal, Value
from amaranth.lib.wiring import Out
from ivga1.util.uart import UART


class StatsUART(Elaboratable):
  def __init__(self, values: Mapping[str, Value], divisor: int = 868, *,
               prefix: str = "ivga2", queue_requests: bool = True):
    self.fields = tuple(values)
    self.values = tuple(Value.cast(value) for value in values.values())
    if not self.fields or any(re.fullmatch(r"[a-z][a-z0-9_]*", name) is None for name in self.fields):
      raise ValueError("Snapshot fields must be nonempty lowercase identifiers")
    if any(value.shape().signed or len(value) > 32 for value in self.values):
      raise ValueError("Snapshot fields must be unsigned values of at most 32 bits")
    if re.fullmatch(r"[a-z][a-z0-9]*", prefix) is None:
      raise ValueError("Invalid snapshot protocol prefix")
    frame_length = len(prefix) + 1 + sum(len(name) + 9 for name in self.fields) + len(self.fields) - 1 + 2
    if frame_length > 4096:
      raise ValueError("Snapshot frame exceeds 4096 bytes")
    self.prefix = prefix
    self.queue_requests = queue_requests
    self.uart = UART(divisor)
    self.rx = self.uart.rx
    self.tx = self.uart.tx
    self.request = Signal()
    self.sending = Signal()
    self.pending = Signal()
    self.snapshot = tuple(Signal(32, name=f"snapshot_{name}") for name in self.fields)

  def elaborate(self, platform):
    m = Module()
    m.submodules.uart = uart = self.uart
    characters: list[Value] = [Const(ord(char), 8) for char in self.prefix + " "]
    for index, (name, value) in enumerate(zip(self.fields, self.snapshot, strict=True)):
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
    if self.queue_requests:
      with m.If(self.sending & request):
        m.d.sync += self.pending.eq(True)
    with m.If(~self.sending & (request | self.pending)):
      m.d.sync += [self.sending.eq(True), self.pending.eq(False), position.eq(0)]
      for snapshot, value in zip(self.snapshot, self.values, strict=True):
        m.d.sync += snapshot.eq(value)
    with m.Elif(self.sending & uart.tx_ready):
      with m.If(position == len(characters) - 1):
        m.d.sync += self.sending.eq(False)
      with m.Else():
        m.d.sync += position.eq(position + 1)
    return m


def runtime_values(runtime, *, done: Value) -> dict[str, Value]:
  """Read physical component registers, not arbiter clients' routed responses."""
  values = {
    "busy": runtime.busy,
    "done": done,
    "failed": runtime.failed,
    "halted": runtime.halted,
  }
  for name, member in runtime.fault.signature.members.items():
    if member.flow == Out:
      values[f"fault_{name}"] = getattr(runtime.fault, name)

  components = [
    ("pages", runtime.page_alloc),
    ("pairs", runtime.pair_alloc),
    ("interact", runtime.interact),
    ("entry_linker", runtime.entry_linker),
    *((f"linker_{i}", linker) for i, linker in enumerate(runtime.interact.linkers)),
  ]
  for prefix, component in components:
    for name in component.control.signature.members:
      if name.startswith("stat_") and name != "stat_reset":
        values[f"{prefix}_{name.removeprefix('stat_')}"] = getattr(component.control, name)

  values.update({
    "pages_empty_id": runtime.page_alloc.empty_id,
    "pages_free_count": runtime.page_alloc.free_count,
    "pairs_pages_owned": runtime.pair_alloc.pages_owned,
    "pairs_free_count": runtime.pair_alloc.free_count,
    "pairs_live_words": runtime.pair_alloc.live_words,
    "active_depth": runtime.active.top,
    "cycles": runtime.cycles,
  })
  return values
