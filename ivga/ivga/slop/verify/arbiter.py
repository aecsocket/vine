"""Actual-DUT round-robin and resource-routing optimization baseline.

Run from ivga/ with ``python -m ivga.slop.verify.arbiter``. Only the runner
creates a clock domain; all histories start at configuration-time init.

Environment contracts are deliberately local:
* Safety gives Arbiter arbitrary request/take, including take with no grant.
  Registered clients hold each pending request until its own acceptance.
  Combinational clients are unrestricted: stall stability is conditional on
  unchanged requests, and a cover demonstrates preemption by a new requester.
* Fairness holds one symbolic client's request continuously, holds other pending
  requests until accepted, and permits at most READY_GAP-1 consecutive not-ready
  cycles. Take is the ready/valid handshake, NOT an assumption of progress.
* Resource clients hold pending commands until their own granted handshake.
  The symbolic resource may stall arbitrarily and returns exactly one response
  response_latency edges after each acceptance. No real heap is instantiated.

Safety and bounded-wait fairness are inductive. External resource scoreboards
are explicitly bounded (see case modes); their independently initialized model
is not claimed to be an inductive invariant of the hidden arbiter state.
No assumption constrains priority, winner, response ownership, or wait age.
"""

import sys
from functools import partial

from amaranth import (
  Array,
  Assert,
  Assume,
  Cat,
  Const,
  Cover,
  Module,
  Mux,
  Signal,
)
from amaranth.asserts import AnyConst, AnySeq
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga.slop.verify.core import FormalCase, run_case
from ivga.util.arbiter import Arbiter, make_arbiter_type

WIDTHS = (1, 2, 3, 5, 15)
READY_GAP = 2


def _scan(m, request, priority, width, *, name="scan"):
  """Independent reference: visit clients in cyclic order, first request wins.

  Intentionally no masks, low-bit isolation, negation, or DUT winner signals.
  Static rotations also avoid a wide divider or variable shift in the oracle.
  """
  winner = Signal(width, name=name)
  with m.Switch(priority):
    for start in range(width):
      with m.Case(start):
        for offset in range(width):
          index = (start + offset) % width
          branch = m.If if offset == 0 else m.Elif
          with branch(request[index]):
            m.d.comb += winner.eq(1 << index)
  return winner


def _index(m, onehot, width, *, name="index"):
  index = Signal(range(width), name=name)
  for i in range(width):
    with m.If(onehot[i]):
      m.d.comb += index.eq(i)
  return index


def _onehot0(value):
  # Cardinality check, independent of the DUT's low-bit-isolation trick.
  return sum(value[i] for i in range(len(value))) <= 1


def _history(m, **values):
  valid = Signal(name="past_valid")
  m.d.sync += valid.eq(1)
  past = {}
  for name, value in values.items():
    past[name] = Signal.like(value, name=f"past_{name}")
    m.d.sync += past[name].eq(value)
  return valid, past


def _hold_requests(m, valid, request, previous_request, previous_accept):
  # The client, not the DUT, promises not to withdraw an unaccepted command.
  with m.If(valid):
    m.d.comb += Assume((request & previous_request & ~previous_accept)
                       == (previous_request & ~previous_accept))


def _safety(width, registered):
  m = Module()
  dut = m.submodules.dut = Arbiter(width, register_grant=registered)
  m.d.comb += [dut.request.eq(AnySeq(width)), dut.take.eq(AnySeq(1))]
  accepted = Signal(width)
  m.d.comb += accepted.eq(Mux(dut.take, dut.grant, 0))
  valid, p = _history(m, request=dut.request, take=dut.take,
                      grant=dut.grant, priority=dut.priority, accepted=accepted)
  if registered:
    _hold_requests(m, valid, dut.request, p["request"], p["accepted"])

  m.d.comb += [
    Assert(_onehot0(dut.grant)),
    Assert(dut.priority < width),
    Assert((dut.grant & ~dut.request) == 0),
  ]
  previous_winner = _scan(m, p["request"], p["priority"], width,
                          name="previous_winner")
  previous_index = _index(m, p["grant"], width, name="previous_index")
  with m.If(~valid):
    m.d.comb += Assert(dut.priority == 0)
    if registered:
      m.d.comb += Assert(dut.grant == 0)
  with m.Else():
    # Selection alone, a stall, and take-without-grant must NOT rotate priority.
    next_priority = Mux(previous_index == width - 1, 0, previous_index + 1)
    m.d.comb += Assert(dut.priority == Mux(p["accepted"].any(),
                                         next_priority, p["priority"]))
    if registered:
      m.d.comb += Assert(dut.grant == Mux(p["take"], 0,
        Mux(p["grant"].any(), p["grant"], previous_winner)))
      with m.If(p["grant"].any() & ~p["take"]):
        m.d.comb += Assert(dut.grant == p["grant"])
      with m.If(p["accepted"].any()):
        # Acceptance clears the slot: it cannot select its replacement too.
        m.d.comb += [Assert(dut.grant == 0), Assert(~accepted.any())]
      with m.If(dut.grant.any() & ~p["grant"].any()):
        m.d.comb += Assert(~p["accepted"].any())
    else:
      with m.If(~p["take"] & (dut.request == p["request"])):
        m.d.comb += Assert(dut.grant == p["grant"])

  if not registered:
    winner = _scan(m, dut.request, dut.priority, width)
    m.d.comb += [Assert(dut.grant == winner),
                 Assert(dut.grant.any() == dut.request.any())]
    if width > 1:
      # Even held pending requests do not imply combinational stall stability:
      # a newly arriving higher-priority client may preempt the old winner.
      m.d.comb += Cover(valid & p["grant"].any() & ~p["take"]
        & ((dut.request & p["request"]) == p["request"])
        & (dut.grant != p["grant"]))
  elif width > 1:
    # A later arrival cannot preempt an already registered winner.
    m.d.comb += Cover(valid & p["grant"].any() & ~p["take"]
      & (dut.request != p["request"]) & (dut.grant == p["grant"]))

  m.d.comb += [
    Cover(valid & p["grant"][width - 1] & ~p["take"] & accepted[width - 1]),
    Cover(valid & p["accepted"][width - 1] & (dut.priority == 0)),
    Cover(valid & ~p["request"].any() & dut.request.any()),
  ]
  if width > 1:
    # Lower-index winner with nonzero priority exercises cyclic wraparound.
    m.d.comb += Cover((dut.priority != 0) & accepted[0])
  if registered:
    m.d.comb += Cover(valid & p["accepted"].any() & dut.request.any()
                      & ~dut.grant.any())
  return m


def _fairness(width, registered):
  m = Module()
  dut = m.submodules.dut = Arbiter(width, register_grant=registered)
  target = AnyConst(max(1, (width - 1).bit_length()))
  ready = AnySeq(1)
  accepted = Signal(width)
  m.d.comb += [
    dut.request.eq(AnySeq(width)),
    dut.take.eq(ready & dut.grant.any()),
    accepted.eq(Mux(dut.take, dut.grant, 0)),
    Assume(target < width),
    Assume(dut.request.bit_select(target, 1)),
  ]
  valid, p = _history(m, request=dut.request, accepted=accepted)
  _hold_requests(m, valid, dut.request, p["request"], p["accepted"])

  not_ready = Signal(range(READY_GAP))
  # Ready must occur in every READY_GAP-cycle sliding window, including startup.
  m.d.comb += Assume((not_ready < READY_GAP - 1) | ready)
  m.d.sync += not_ready.eq(Mux(ready, 0, not_ready + 1))

  # Each of at most width services costs READY_GAP cycles, plus a selection
  # cycle for a registered grant. A continually requesting client cannot be
  # bypassed by more than width-1 accepted commands in any round.
  bound = width * (READY_GAP + int(registered))
  age = Signal(range(bound + 1))
  served = accepted.bit_select(target, 1)
  m.d.sync += age.eq(Mux(served, 0, age + 1))
  m.d.comb += Assert(age < bound)

  bypasses = Signal(range(width + 1))
  with m.If(served):
    m.d.sync += bypasses.eq(0)
  with m.Elif(accepted.any()):
    m.d.sync += bypasses.eq(bypasses + 1)
  m.d.comb += Assert(bypasses < width)

  # Checked progress lemmas, not assumptions: each other acceptance consumes
  # cyclic distance to the target; each service interval costs at most `cost`.
  # They keep the 15-client search from enumerating exponentially many rounds.
  distance = Signal(range(width), name="distance_to_target")
  m.d.comb += distance.eq(Mux(target >= dut.priority, target - dut.priority,
                              target + width - dut.priority))
  m.d.comb += Assert(bypasses + distance < width)
  cost = READY_GAP + int(registered)
  service_age = Signal(range(cost + 1))
  m.d.sync += service_age.eq(Mux(accepted.any(), 0, service_age + 1))
  m.d.comb += [Assert(service_age < cost),
               Assert(age <= bypasses * cost + service_age)]
  m.d.comb += [
    Cover(served & (age == bound - 1)),
    Cover((dut.request == (1 << width) - 1) & ~ready & dut.grant.any()),
  ]
  if width > 1:
    m.d.comb += Cover(served & (bypasses == width - 1))
  return m


class _ResourcePort(wiring.Signature):
  """Small configurable resource, with broadcast and deliberately excluded IO."""

  def __init__(self, data_width):
    super().__init__({
      "request": Out(1),
      "write": Out(1),
      "address": Out(3),
      "data": Out(data_width),
      "excluded_out": Out(2),
      "granted": In(1),
      "response_valid": In(1),
      "result": In(data_width),
      "status": In(2),
      "excluded_in": In(2),
    })


def _resource(width, latency, registered):
  m = Module()
  data_width = width + 2
  resource_type = make_arbiter_type(
    _ResourcePort, lambda client: client.request,
    always_out=("status",), exclude=("excluded_out", "excluded_in"),
    response_latency=latency, register_grant=registered,
  )
  dut = m.submodules.dut = resource_type(width, data_width)
  ready = AnySeq(1)
  m.d.comb += [dut.real.granted.eq(ready), dut.real.status.eq(AnySeq(2)),
               dut.real.excluded_in.eq(AnySeq(2))]
  for client in dut.clients:
    for field in ("request", "write", "address", "data", "excluded_out"):
      signal = getattr(client, field)
      m.d.comb += signal.eq(AnySeq(len(signal)))

  request = Cat(*(client.request for client in dut.clients))
  actual_accept = Cat(*(client.granted for client in dut.clients))
  commands = [Cat(client.write, client.address, client.data) for client in dut.clients]
  valid, p = _history(m, request=request, accepted=actual_accept,
                      command=Cat(*commands), real_command=Cat(
                        dut.real.request, dut.real.write,
                        dut.real.address, dut.real.data))
  _hold_requests(m, valid, request, p["request"], p["accepted"])
  command_width = len(commands[0])
  for i, command in enumerate(commands):
    with m.If(valid & p["request"][i] & ~p["accepted"][i]):
      m.d.comb += Assume(command == p["command"].word_select(i, command_width))

  # Independent protocol-level scheduler, using only symbolic client inputs and
  # ready, not the DUT's grant, internal arbiter, owner, or outgoing command.
  priority = Signal(range(width), name="reference_priority")
  winner = _scan(m, request, priority, width)
  if registered:
    selected = Signal(width, name="reference_pending")
    with m.If(selected.any() & ready):
      m.d.sync += selected.eq(0)
    with m.Elif(~selected.any()):
      m.d.sync += selected.eq(winner)
  else:
    selected = winner
  index = _index(m, selected, width, name="selected_index")
  accept = selected.any() & ready
  with m.If(accept):
    m.d.sync += priority.eq(Mux(index == width - 1, 0, index + 1))

  for field in ("request", "write", "address", "data"):
    expected = Const(0, len(getattr(dut.real, field)))
    for i, client in enumerate(dut.clients):
      expected = Mux(selected[i], getattr(client, field), expected)
    m.d.comb += Assert(getattr(dut.real, field) == expected)
  m.d.comb += [Assert(_onehot0(actual_accept)),
               Assert(actual_accept == Mux(ready, selected, 0)),
               Assert(dut.real.excluded_out == 0)]

  # Symbolic, fixed-latency resource: the response fingerprints all command
  # fields. This is not a heap or an assumption about the DUT's response path.
  def fingerprint(write, address, data):
    return data ^ address ^ Mux(write, Const((1 << data_width) - 1, data_width), 0)

  response_valid = dut.real.request & ready
  response_data = fingerprint(dut.real.write, dut.real.address, dut.real.data)
  expected_valid = accept
  expected_data = fingerprint(Array(c.write for c in dut.clients)[index],
                              Array(c.address for c in dut.clients)[index],
                              Array(c.data for c in dut.clients)[index])
  owner = Signal(range(width), name="reference_owner")
  with m.If(accept):
    m.d.sync += owner.eq(index)
  m.d.comb += Assert(dut.owner == owner)
  response_owner = owner
  for stage in range(latency):
    stub_valid = Signal(name=f"stub_valid_{stage}")
    stub_data = Signal(data_width, name=f"stub_data_{stage}")
    check_valid = Signal(name=f"expected_valid_{stage}")
    check_data = Signal(data_width, name=f"expected_data_{stage}")
    m.d.sync += [stub_valid.eq(response_valid), stub_data.eq(response_data),
                 check_valid.eq(expected_valid), check_data.eq(expected_data)]
    response_valid, response_data = stub_valid, stub_data
    expected_valid, expected_data = check_valid, check_data
    if stage < latency - 1:
      delayed_owner = Signal(range(width), name=f"expected_owner_{stage}")
      m.d.sync += delayed_owner.eq(response_owner)
      response_owner = delayed_owner
  m.d.comb += [dut.real.response_valid.eq(response_valid),
               dut.real.result.eq(response_data)]
  for i, client in enumerate(dut.clients):
    m.d.comb += [
      Assert(client.granted == (selected[i] & ready)),
      Assert(~client.granted | client.request),
      Assert(client.status == dut.real.status),
      Assert(client.excluded_in == 0),
      # Ownership and isolation are checked even during invalid/bubble cycles.
      Assert(client.result == Mux(response_owner == i, response_data, 0)),
      Assert(client.response_valid == (expected_valid & (response_owner == i))),
    ]
    with m.If(client.response_valid):
      m.d.comb += Assert(client.result == expected_data)

  past_valid, q = _history(m, selected=selected, accept=accept,
                           stalled=dut.real.request & ~ready)
  with m.If(past_valid & q["accept"]):
    if registered:
      m.d.comb += Assert(~actual_accept.any())
  with m.If(valid & q["stalled"]):
    if registered:
      m.d.comb += Assert(Cat(dut.real.request, dut.real.write,
                            dut.real.address, dut.real.data) == p["real_command"])
    else:
      # Combinational selection may be preempted by a new higher-priority input.
      with m.If(selected == q["selected"]):
        m.d.comb += Assert(Cat(dut.real.request, dut.real.write,
                              dut.real.address, dut.real.data) == p["real_command"])

  m.d.comb += [
    Cover(past_valid & q["stalled"] & actual_accept[width - 1]),
    Cover(expected_valid & (response_owner == width - 1) & response_data.any()),
    Cover(expected_valid & ~dut.real.request),
    Cover(~request.any() & dut.real.status.any()),
  ]
  if latency == 2:
    # A response to one client overlaps acceptance by a DIFFERENT client. This
    # detects routing by newest owner instead of the acceptance-edge owner.
    m.d.comb += Cover(expected_valid & accept & (response_owner != index))
  else:
    responded = Signal(width, name="responded")
    for i in range(width):
      with m.If(expected_valid & (response_owner == i)):
        m.d.sync += responded[i].eq(1)
    m.d.comb += Cover(responded[0] & expected_valid & (response_owner == width - 1))
  return m


def cases() -> list[FormalCase]:
  result = []
  for registered in (False, True):
    kind = "registered" if registered else "comb"
    for width in WIDTHS:
      result.append(FormalCase(
        f"arbiter_{kind}_w{width}_safety", partial(_safety, width, registered),
        depth=8, mode="prove", timeout=30,
      ))
      bound = width * (READY_GAP + int(registered))
      result.append(FormalCase(
        f"arbiter_{kind}_w{width}_fairness", partial(_fairness, width, registered),
        depth=bound + 4, mode="prove", timeout=120 if width == 15 else 30,
      ))
    for width in (2, 3):
      for latency in (1, 2):
        result.append(FormalCase(
          f"resource_arbiter_{kind}_w{width}_latency{latency}",
          partial(_resource, width, latency, registered),
          depth=20, mode="bmc", timeout=30,
        ))
  return result


if __name__ == "__main__":
  requested = set(sys.argv[1:])
  suite = cases()
  unknown = requested - {case.name for case in suite}
  if unknown:
    raise SystemExit(f"Unknown cases: {', '.join(sorted(unknown))}")
  failed = False
  for case in suite:
    if requested and case.name not in requested:
      continue
    result = run_case(case)
    print(f"{case.name}: {result.detail} ({result.seconds:.2f}s)", flush=True)
    failed |= not result.passed
  raise SystemExit(int(failed))