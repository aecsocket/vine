from amaranth import Module
from amaranth.lib import wiring

from ivga.runtime.runtime import Runtime


class Processor(wiring.Component):
  """Top-level wrapper exposing the runtime's control and host interfaces."""

  def __init__(self, *, active_depth: int = 128, reserved_pages: int = 0):
    self.runtime = Runtime(active_depth=active_depth, reserved_pages=reserved_pages)
    super().__init__(self.runtime.signature)

  def elaborate(self, platform):
    m = Module()
    m.submodules.runtime = self.runtime
    wiring.connect(m, wiring.flipped(self), self.runtime)
    return m
