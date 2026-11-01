from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out


class FaultPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "halt": In(1),
      # machine ran out of memory
      "oom": Out(1),
      # attempted to push active pair onto the worklist,
      # but the stack was full
      "active_full": Out(1),
      # copied an extrinsic value which is non-copyable
      "ext_copy": Out(1),
      # supplied extrinsic values of incorrect types to
      # extrinsic function call
      "ext_inputs": Out(1),
      # extrinsic function failed
      "ext_generic": Out(1),
      # interacted two ports of tags which may not interact
      "invalid_interaction": Out(1),
    })
