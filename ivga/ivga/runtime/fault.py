from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out


class FaultPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "halt": In(1),
      "oom": Out(1),
      "active_full": Out(1),
      "ext_copy": Out(1),
      "invalid_interaction": Out(1),
    })
