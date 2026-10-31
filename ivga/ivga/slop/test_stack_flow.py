"""Stack flow simulations: run ``python -m ivga.slop.test_stack_flow``."""

import unittest

from amaranth import Const, Module, Signal, Value, signed, unsigned
from amaranth.lib import wiring
from amaranth.sim import Simulator
from amflow import expand

from ivga import Port2
from ivga.mem.stack import Stack, StackArbiter, StackPort

CLOCK_PERIOD = 1e-6
DEFAULT_SHAPE = unsigned(8)


def add_flow(m, generator):
  def flow(f):
    yield from generator(f)
    f.d.until += Const(0)
    yield
  expand(m, lambda f: flow(f))


def setup(shape=DEFAULT_SHAPE, depth=8, arbitrate=False):
  m = Module()
  stack = m.submodules.stack = Stack(shape, depth)
  arb = None
  if arbitrate:
    arb = m.submodules.arb = StackArbiter(2, shape)
    wiring.connect(m, arb.real, stack.control)
  return m, stack, arb


class StackFlowTests(unittest.TestCase):
  def simulate(self, m, bench):
    sim = Simulator(m)
    sim.add_clock(CLOCK_PERIOD)
    sim.add_testbench(bench)
    sim.run()

  def check_result_shape(self, result, stack):
    self.assertEqual(result.shape(), stack.pop_data.shape())
    self.assertEqual(Value.cast(result).shape(), Value.cast(stack.pop_data).shape())
    if stack.shape is Port2:
      self.assertEqual(result.a.shape(), stack.pop_data.a.shape())
      self.assertEqual(result.b.shape(), stack.pop_data.b.shape())
      self.assertEqual(result.a.payload.shape(), stack.pop_data.a.payload.shape())
      self.assertEqual(result.b.label.shape(), stack.pop_data.b.label.shape())

  def check_lifo(self, shape, values, *, control=False):
    m, stack, _ = setup(shape, depth=len(values))
    port = stack.control if control else stack
    finished = Signal()
    pops = []
    captured = [Signal.like(stack.pop_data) for _ in values]

    def flow(f):
      for value in values:
        result = yield StackPort.push(f, port, data=value)
        self.assertIsNone(result)
      for _ in values:
        result = StackPort.pop(f, port)
        returned = yield result
        self.assertIs(returned, result)
        self.check_result_shape(returned, stack)
        pops.append(returned)
      f.d.sync += [
        *(out.eq(result) for out, result in zip(captured, pops)),
        finished.eq(1),
      ]
      yield

    add_flow(m, flow)

    async def bench(ctx):
      for index, value in enumerate(values):
        self.assertEqual(ctx.get(stack.top), index)
        self.assertEqual(ctx.get(stack.req_push), 1, "bubble between pushes")
        self.assertEqual(ctx.get(stack.req_pop), 0)
        self.assertEqual(ctx.get(Value.cast(stack.push_data)), value)
        self.assertEqual(ctx.get(stack.granted), 1)
        await ctx.tick()
      expected = list(reversed(values))
      for index, result in enumerate(pops):
        self.assertEqual(ctx.get(stack.top), len(values) - index)
        self.assertEqual(ctx.get(stack.req_push), 0)
        self.assertEqual(ctx.get(stack.req_pop), 1, "bubble between pops")
        self.assertEqual(ctx.get(stack.granted), 1)
        await ctx.tick()
        # Check before any further edge can save the synchronous RAM response.
        self.assertEqual(ctx.get(Value.cast(result)), expected[index])
        for previous in range(index + 1):
          self.assertEqual(ctx.get(Value.cast(pops[previous])), expected[previous])
      self.assertEqual(ctx.get(finished), 0)
      await ctx.tick()
      for _ in range(5):
        self.assertEqual(ctx.get(finished), 1)
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(stack.req_push), 0)
        self.assertEqual(ctx.get(stack.req_pop), 0)
        for index, result in enumerate(pops):
          self.assertEqual(ctx.get(Value.cast(result)), expected[index])
          self.assertEqual(ctx.get(Value.cast(captured[index])), expected[index])
          if shape is Port2:
            self.assertEqual(ctx.get(Value.cast(result.a)),
                             expected[index] & ((1 << 64) - 1))
            self.assertEqual(ctx.get(Value.cast(result.b)), expected[index] >> 64)
            self.assertEqual(ctx.get(result.a.payload),
                             (expected[index] >> 3) & ((1 << 45) - 1))
            self.assertEqual(ctx.get(result.b.label), expected[index] >> 112)
        await ctx.tick()

    self.simulate(m, bench)

  def test_direct_lifo_no_bubbles(self):
    for control in (False, True):
      with self.subTest(control=control):
        self.check_lifo(unsigned(8), [0x12, 0xA5, 0, 0xFF], control=control)

  def test_port2_packed_128_bit_results(self):
    values = [
      0xFEDCBA98765432100123456789ABCDEF,
      0x80000000000000010000000000000002,
      0x13579BDF2468ACE0F0E1D2C3B4A59687,
    ]
    self.check_lifo(Port2, values, control=True)

  def test_signed_results(self):
    self.check_lifo(signed(8), [-128, 127, -7, 0])

  def test_consecutive_pop_addition_and_retention(self):
    m, stack, _ = setup(depth=4)
    pops = []
    total = Signal(9)
    later_total = Signal(9)
    finished = Signal()

    def flow(f):
      for value in [0x19, 0x62, 0xA4, 0xE7]:
        yield StackPort.push(f, stack, data=value)
      first = yield StackPort.pop(f, stack)
      pops.append(first)
      second = yield StackPort.pop(f, stack.control)
      pops.append(second)
      f.d.sync += total.eq(first + second)
      yield
      third = yield StackPort.pop(f, stack)
      pops.append(third)
      fourth = yield StackPort.pop(f, stack.control)
      pops.append(fourth)
      f.d.sync += [later_total.eq(first + second), finished.eq(1)]
      yield

    add_flow(m, flow)

    async def bench(ctx):
      first, second, third, fourth = pops
      await ctx.tick().repeat(4)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(first), 0xE7)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(second), 0xA4)
      self.assertEqual(ctx.get(first + second), 0x18B)
      await ctx.tick()
      self.assertEqual(ctx.get(total), 0x18B)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(third), 0x62)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(fourth), 0x19)
      await ctx.tick()
      for _ in range(5):
        self.assertEqual(ctx.get(finished), 1)
        self.assertEqual(ctx.get(total), 0x18B)
        self.assertEqual(ctx.get(later_total), 0x18B)
        for result, value in zip(pops, [0xE7, 0xA4, 0x62, 0x19]):
          self.assertEqual(ctx.get(result), value)
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(stack.req_push | stack.req_pop), 0)
        await ctx.tick()

    self.simulate(m, bench)

  def test_arbitration_holds_requests_and_preserves_data(self):
    m, stack, arb = setup(arbitrate=True)
    assert arb is not None
    ready = [Signal(name=f"ready_{i}") for i in range(2)]
    finished = [Signal(name=f"finished_{i}") for i in range(2)]
    values = [[0x11, 0x23, 0x45, 0x67], [0x89, 0xAB, 0xCD, 0xEF]]
    pops = []

    def flow(f, client, port, client_pops):
      for value in values[client]:
        yield StackPort.push(f, port, data=value)
      # Neither client pops until both have finished all four pushes.
      f.d.sync += ready[client].eq(1)
      yield
      f.d.until += ready[0] & ready[1]
      yield
      for _ in range(4):
        result = yield StackPort.pop(f, port)
        self.check_result_shape(result, stack)
        client_pops.append(result)
      f.d.sync += finished[client].eq(1)
      yield

    for client, port in enumerate(arb.clients):
      client_pops = []
      pops.append(client_pops)
      flow_m = m.submodules[f"flow_{client}"] = Module()
      add_flow(flow_m,
               lambda f, client=client, port=port, client_pops=client_pops:
                 flow(f, client, port, client_pops))

    async def bench(ctx):
      oracle = []
      accepted = [0, 0]
      denied = {"push": 0, "pop": 0}
      held: list[tuple[int, int, int | None] | None] = [None, None]
      retained = []
      for cycle in range(40):
        self.assertEqual(ctx.get(stack.top), len(oracle), f"cycle {cycle}")
        self.assertEqual(ctx.get(stack.empty), not oracle)
        self.assertEqual(ctx.get(stack.full), len(oracle) == stack.depth)
        responses = []
        grants = 0
        for client, port in enumerate(arb.clients):
          push = ctx.get(port.req_push)
          pop = ctx.get(port.req_pop)
          self.assertFalse(push and pop)
          request = (push, pop, ctx.get(port.push_data) if push else None)
          if held[client] is not None:
            self.assertEqual(request, held[client], "denied request changed")
          held[client] = None
          if not (push or pop):
            self.assertEqual(ctx.get(port.granted), 0)
            continue
          index = accepted[client]
          self.assertLess(index, 8, "operation was repeated")
          self.assertEqual(push, index < 4)
          if push:
            self.assertEqual(ctx.get(port.push_data), values[client][index])
          if not ctx.get(port.granted):
            denied["push" if push else "pop"] += 1
            held[client] = request
            continue
          grants += 1
          if push:
            self.assertLess(len(oracle), stack.depth)
            oracle.append(values[client][index])
          else:
            self.assertTrue(oracle, "pop accepted while empty")
            value = oracle.pop()
            result = pops[client][index - 4]
            responses.append((port, result, value))
          accepted[client] += 1
        self.assertLessEqual(grants, 1)
        self.assertEqual(ctx.get(stack.granted), grants)
        await ctx.tick()
        for port, result, value in responses:
          self.assertEqual(ctx.get(port.pop_data), value, "RAM/owner response")
          self.assertEqual(ctx.get(result), value, "live response")
          retained.append((result, value))
        for result, value in retained:
          self.assertEqual(ctx.get(result), value, "saved response")
      self.assertEqual(accepted, [8, 8])
      self.assertGreater(denied["push"], 0)
      self.assertGreater(denied["pop"], 0)
      self.assertEqual(oracle, [])
      self.assertEqual(ctx.get(stack.top), 0)
      self.assertEqual([ctx.get(flag) for flag in finished], [1, 1])

    self.simulate(m, bench)

  async def raw_push(self, ctx, port, value):
    self.assertEqual(ctx.get(port.full), 0)
    ctx.set(port.push_data, value)
    ctx.set(port.req_push, 1)
    self.assertEqual(ctx.get(port.granted), 1)
    await ctx.tick()
    ctx.set(port.req_push, 0)

  async def raw_pop(self, ctx, port, expected):
    self.assertEqual(ctx.get(port.empty), 0)
    ctx.set(port.req_pop, 1)
    self.assertEqual(ctx.get(port.granted), 1)
    await ctx.tick()
    ctx.set(port.req_pop, 0)
    self.assertEqual(ctx.get(port.pop_data), expected)

  def test_full_push_waits_for_another_client_pop(self):
    m, stack, arb = setup(depth=2, arbitrate=True)
    assert arb is not None
    raw, port = arb.clients
    start = Signal()
    finished = Signal()

    def flow(f):
      f.d.until += start
      yield
      yield StackPort.push(f, port, data=0xC3)
      f.d.sync += finished.eq(1)
      yield

    add_flow(m, flow)

    async def bench(ctx):
      await self.raw_push(ctx, raw, 0x17)
      await self.raw_push(ctx, raw, 0x92)
      ctx.set(start, 1)
      await ctx.tick()
      for _ in range(4):
        self.assertEqual(ctx.get(stack.top), 2)
        self.assertEqual(ctx.get(port.full), 1)
        self.assertEqual(ctx.get(port.req_push | port.req_pop), 0)
        self.assertEqual(ctx.get(finished), 0)
        await ctx.tick()
      await self.raw_pop(ctx, raw, 0x92)
      self.assertEqual(ctx.get(stack.top), 1)
      self.assertEqual(ctx.get(port.req_push), 1)
      self.assertEqual(ctx.get(port.granted), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(stack.top), 2)
      self.assertEqual(ctx.get(port.req_push), 0)
      await ctx.tick()
      self.assertEqual(ctx.get(finished), 1)
      # Read RAM back: seeing the correct push bus alone is insufficient.
      await self.raw_pop(ctx, raw, 0xC3)
      await self.raw_pop(ctx, raw, 0x17)
      for _ in range(5):
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(port.req_push | port.req_pop), 0)
        await ctx.tick()

    self.simulate(m, bench)

  def test_empty_pop_waits_for_another_client_push(self):
    m, stack, arb = setup(depth=2, arbitrate=True)
    assert arb is not None
    raw, port = arb.clients
    pops = []
    captured = Signal(8)
    finished = Signal()

    def flow(f):
      result = yield StackPort.pop(f, port)
      self.check_result_shape(result, stack)
      pops.append(result)
      f.d.sync += [captured.eq(result), finished.eq(1)]
      yield

    add_flow(m, flow)

    async def bench(ctx):
      result, = pops
      for _ in range(4):
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(port.empty), 1)
        self.assertEqual(ctx.get(port.req_push | port.req_pop), 0)
        self.assertEqual(ctx.get(finished), 0)
        await ctx.tick()
      await self.raw_push(ctx, raw, 0xD6)
      self.assertEqual(ctx.get(port.req_pop), 1)
      self.assertEqual(ctx.get(port.granted), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(stack.top), 0)
      self.assertEqual(ctx.get(result), 0xD6)
      self.assertEqual(ctx.get(port.req_pop), 0)
      await ctx.tick()
      self.assertEqual(ctx.get(finished), 1)
      self.assertEqual(ctx.get(captured), 0xD6)
      # Change both RAM's output and arbiter response ownership after saving.
      await self.raw_push(ctx, raw, 0x39)
      await self.raw_pop(ctx, raw, 0x39)
      for _ in range(5):
        self.assertEqual(ctx.get(result), 0xD6)
        self.assertEqual(ctx.get(captured), 0xD6)
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(port.req_push | port.req_pop), 0)
        await ctx.tick()

    self.simulate(m, bench)


if __name__ == "__main__":
  _ = unittest.main(verbosity=2)
