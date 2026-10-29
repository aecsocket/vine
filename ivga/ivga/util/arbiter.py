from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga import flatten


class ArbiterPort(wiring.Signature):
  def __init__(self, width: int):
    super().__init__({
      "request": Out(width),
      "take": Out(1),
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
      with m.If(self.take & grant[client]):
        m.d.sync += priority.eq(0 if client == N - 1 else client + 1)

    return m

def make_arbiter_type[T: wiring.Signature](
  port_type: type[T],
  client_wants: Callable[[Any], Value],  # pyright: ignore[reportExplicitAny]
  *,
  always_out: tuple[str, ...] = (),
  exclude: tuple[str, ...] = (),
) -> type[wiring.Component]:
  class ResourceArbiter(wiring.Component):
    def __init__(self, width, *port_args, **port_kwargs):
      self.width = width
      self.owner = Signal(range(width))
      super().__init__({
        "clients": In(port_type(*port_args, **port_kwargs)).array(width),
        "real": Out(port_type(*port_args, **port_kwargs)),
      })

    def elaborate(self, platform):
      m = Module()
      arbiter = m.submodules.arbiter = Arbiter(self.width)

      for i, client in enumerate(self.clients):
        wants = client_wants(client)
        m.d.comb += arbiter.request[i].eq(wants)

        for name in always_out:
          m.d.comb += getattr(client, name).eq(getattr(self.real, name))

        with m.If(arbiter.grant[i]):
          connect_outputs(
            m,
            wiring.flipped(client),
            wiring.flipped(self.real),
            exclude=exclude,
          )
          m.d.comb += client.granted.eq(self.real.granted)
          with m.If(self.real.granted):
            m.d.sync += self.owner.eq(i)
            m.d.comb += arbiter.take.eq(True)

        with m.If(self.owner == i):
          connect_inputs(
            m,
            wiring.flipped(client),
            wiring.flipped(self.real),
            exclude=("granted", *always_out, *exclude),
          )

      return m

  return ResourceArbiter



def connect_outputs(m, a, b, *, exclude=()):
  """Connect `a`'s `Out` ports to `b`'s matching `In` ports in the `comb` domain.

  Similar to `wiring.connect`, but only for `Out` to `In`.

  Use `exclude` to specify field members to skip wiring.
  """
  _connect_direction(m, a, b, Out, exclude)


def connect_inputs(m, a, b, *, exclude=()):
  """Connect `a`'s `In` ports from `b`'s matching `Out` ports in the `comb` domain.

  See `connect_outputs`.
  """
  _connect_direction(m, a, b, In, exclude)


def _connect_direction(m, a, b, flow, exclude):
  if isinstance(exclude, str):
    exclude = (exclude,)
  exclude = set(exclude)
  unknown = exclude - set(a.signature.members)
  if unknown:
    raise ValueError(f"Unknown excluded members: {sorted(unknown)}")

  other = {path: (member, value) for path, member, value in b.signature.flatten(b)}
  members_a, members_b = {}, {}
  values_a, values_b = {}, {}
  for path, member, value in a.signature.flatten(a):
    if member.flow != flow or path[0] in exclude:
      continue
    if path not in other:
      raise wiring.ConnectionError(f"Missing matching port at {path!r}")
    other_member, other_value = other[path]
    if other_member.flow == member.flow:
      raise wiring.ConnectionError(f"Expected opposite port directions at {path!r}")
    name = f"port_{len(members_a)}"
    members_a[name], members_b[name] = member, other_member
    values_a[name], values_b[name] = value, other_value

  if members_a:
    # Views reuse the original signals; connect still checks widths, init, and constants.
    wiring.connect(m,
      SimpleNamespace(signature=wiring.Signature(members_a), **values_a),
      SimpleNamespace(signature=wiring.Signature(members_b), **values_b),
    )
