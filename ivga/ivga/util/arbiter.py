from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga import flatten


class ArbiterPort(wiring.Signature):
  def __init__(self, width: int):
    super().__init__({
      "request": Out(width),
      "grant": In(width),
    })

class Arbiter(wiring.Component):
  """
  Round-robin resource arbiter.

  Assuming we have clients A, B, C, priority works like so:
  - Priority starts at A.
  - If only A wants the resource, A gets the grant and priority moves to B.
  - Or, if A and another client wants the resource, A gets the grant and priority
    moves to B.
  - If A doesn't want the resource but B and C do, we start from the current
    priority (A) and cycle until we find a slot who wants the resource (B).
    We give them the grant, and bump priority again.
  """

  def __init__(self, width: int):
    assert width > 0
    self.width = width
    self.priority = Signal(range(width))

    super().__init__({
      "control": In(ArbiterPort(width))
    })
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()

    N = self.width
    priority = self.priority
    request = self.request
    grant = self.grant
    mask = Signal(N)
    eligible = Signal(N)
    candidates = Signal(N)
    m.d.comb += [
      # who might get priority right now, _assuming everyone was requesting?_
      # only anyone who is index `priority` or above;
      # if that's nobody, then we consider everyone.
      mask.eq(Const((1 << N) - 1, N) << priority),
      # who might get priority right now, _based on who's actually requesting?_
      eligible.eq(request & mask),
      # if eligible is 0, then e.g. prio is C but we need to let A or B get grant
      # if eligible > 0, then there's someone at or after our prio who will get it
      candidates.eq(Mux(eligible.any(), eligible, request)),
      # give the grant to the single lowest bit out of our candidates
      # candidates = 0110 -> grant = 0010
      grant.eq(candidates & -candidates),
    ]

    for client in range(N):
      with m.If(grant[client]):
        m.d.sync += priority.eq(0 if client == N - 1 else client + 1)

    return m
