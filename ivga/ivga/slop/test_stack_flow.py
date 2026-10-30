"""Stack flow simulations: run ``python -m ivga.slop.test_stack_flow``."""

import unittest

from amaranth import Const, Module, Signal, Value, signed, unsigned
from amaranth.lib import wiring
from amaranth.sim import Simulator

from amflow import Operation, Step, Sync, expand, expand_ops
from ivga import Port2
from ivga.mem.stack import Stack, StackArbiter, StackPop, StackPush


CLOCK_PERIOD = 1e-6


def stop():
  return Step("stop", comb=[], sync=[], until=Const(0))


def add_flow(m, operations, use_expand=False):
  operations = [*operations, stop()]
  if use_expand:
    def flow():
      for operation in operations:
        result = yield operation
        assert result is operation.result
    expand(m, flow())
  else:
    expand_ops(m, operations)


def setup(shape=unsigned(8), depth=8, arbitrate=False):
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

  def check_result_shape(self, operation, stack):
    self.assertIsInstance(operation, Operation)
    self.assertIsInstance(operation.result, Value, "pop result must be packed")
    self.assertEqual(operation.result.shape(), Value.cast(stack.pop_data).shape())

  def check_lifo(self, shape, values, *, control=False, use_expand=False):
    m, stack, _ = setup(shape, depth=len(values))
    port = stack.control if control else stack
    finished = Signal()
    pushes = [StackPush(f"push_{i}", port, value) for i, value in enumerate(values)]
    pops = [StackPop(f"pop_{i}", port) for i in range(len(values))]
    captured = [Signal(Value.cast(stack.pop_data).shape()) for _ in pops]
    for operation in pushes:
      self.assertIsInstance(operation, Operation)
    for operation in pops:
      self.check_result_shape(operation, stack)
    add_flow(m, [
      *pushes, *pops,
      Sync("capture", [
        *(out.eq(operation.result) for out, operation in zip(captured, pops)),
        finished.eq(1),
      ]),
    ], use_expand)

    async def bench(ctx):
      for index, value in enumerate(values):
        self.assertEqual(ctx.get(stack.top), index)
        self.assertEqual(ctx.get(stack.req_push), 1, "bubble between pushes")
        self.assertEqual(ctx.get(stack.req_pop), 0)
        self.assertEqual(ctx.get(Value.cast(stack.push_data)), value)
        self.assertEqual(ctx.get(stack.granted), 1)
        await ctx.tick()
      expected = list(reversed(values))
      for index, operation in enumerate(pops):
        self.assertEqual(ctx.get(stack.top), len(values) - index)
        self.assertEqual(ctx.get(stack.req_push), 0)
        self.assertEqual(ctx.get(stack.req_pop), 1, "bubble between pops")
        self.assertEqual(ctx.get(stack.granted), 1)
        await ctx.tick()
        # Check before any further edge can save the synchronous RAM response.
        self.assertEqual(ctx.get(operation.result), expected[index])
        for previous in range(index + 1):
          self.assertEqual(ctx.get(pops[previous].result), expected[previous])
      self.assertEqual(ctx.get(finished), 0)
      await ctx.tick()
      for _ in range(5):
        self.assertEqual(ctx.get(finished), 1)
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(stack.req_push), 0)
        self.assertEqual(ctx.get(stack.req_pop), 0)
        for index, operation in enumerate(pops):
          self.assertEqual(ctx.get(operation.result), expected[index])
          self.assertEqual(ctx.get(captured[index]), expected[index])
        await ctx.tick()

    self.simulate(m, bench)

  def test_direct_lifo_no_bubbles(self):
    for control in (False, True):
      for use_expand in (False, True):
        with self.subTest(control=control, expand=use_expand):
          self.check_lifo(unsigned(8), [0x12, 0xA5, 0, 0xFF],
                          control=control, use_expand=use_expand)

  def test_port2_packed_128_bit_results(self):
    values = [
      0xFEDCBA98765432100123456789ABCDEF,
      0x80000000000000010000000000000002,
      0x13579BDF2468ACE0F0E1D2C3B4A59687,
    ]
    self.check_lifo(Port2, values, control=True, use_expand=True)

  def test_signed_results(self):
    self.check_lifo(signed(8), [-128, 127, -7, 0], use_expand=True)

  def test_consecutive_pop_addition_and_retention(self):
    m, stack, _ = setup(depth=4)
    first = StackPop("first", stack)
    second = StackPop("second", stack.control)
    third = StackPop("third", stack)
    fourth = StackPop("fourth", stack.control)
    total = Signal(9)
    later_total = Signal(9)
    finished = Signal()
    add_flow(m, [
      *(StackPush(f"push_{i}", stack, value)
        for i, value in enumerate([0x19, 0x62, 0xA4, 0xE7])),
      first, second,
      Sync("add", total.eq(first.result + second.result)),
      third, fourth,
      Sync("add_later", [
        later_total.eq(first.result + second.result), finished.eq(1),
      ]),
    ], use_expand=True)

    async def bench(ctx):
      await ctx.tick().repeat(4)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(first.result), 0xE7)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(second.result), 0xA4)
      self.assertEqual(ctx.get(first.result + second.result), 0x18B)
      await ctx.tick()
      self.assertEqual(ctx.get(total), 0x18B)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(third.result), 0x62)
      self.assertEqual(ctx.get(stack.req_pop), 1)
      await ctx.tick()
      self.assertEqual(ctx.get(fourth.result), 0x19)
      await ctx.tick()
      for _ in range(5):
        self.assertEqual(ctx.get(finished), 1)
        self.assertEqual(ctx.get(total), 0x18B)
        self.assertEqual(ctx.get(later_total), 0x18B)
        for operation, value in zip([first, second, third, fourth],
                                    [0xE7, 0xA4, 0x62, 0x19]):
          self.assertEqual(ctx.get(operation.result), value)
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(stack.req_push | stack.req_pop), 0)
        await ctx.tick()

    self.simulate(m, bench)

  def test_arbitration_holds_requests_and_preserves_data(self):
    m, stack, arb = setup(arbitrate=True)
    ready = [Signal(name=f"ready_{i}") for i in range(2)]
    finished = [Signal(name=f"finished_{i}") for i in range(2)]
    values = [[0x11, 0x23, 0x45, 0x67], [0x89, 0xAB, 0xCD, 0xEF]]
    pops = []
    for client, port in enumerate(arb.clients):
      client_pops = [StackPop(f"pop_{i}", port) for i in range(4)]
      pops.append(client_pops)
      for operation in client_pops:
        self.check_result_shape(operation, stack)
      # Neither client pops until both have finished all four pushes.
      flow_m = m.submodules[f"flow_{client}"] = Module()
      add_flow(flow_m, [
        *(StackPush(f"push_{i}", port, value)
          for i, value in enumerate(values[client])),
        Sync("ready", ready[client].eq(1)),
        Step("barrier", comb=[], sync=[], until=ready[0] & ready[1]),
        *client_pops,
        Sync("finished", finished[client].eq(1)),
      ], use_expand=bool(client))

    async def bench(ctx):
      oracle = []
      accepted = [0, 0]
      denied = {"push": 0, "pop": 0}
      held = [None, None]
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
            operation = pops[client][index - 4]
            responses.append((port, operation, value))
          accepted[client] += 1
        self.assertLessEqual(grants, 1)
        self.assertEqual(ctx.get(stack.granted), grants)
        await ctx.tick()
        for port, operation, value in responses:
          self.assertEqual(ctx.get(port.pop_data), value, "RAM/owner response")
          self.assertEqual(ctx.get(operation.result), value, "live response")
          retained.append((operation, value))
        for operation, value in retained:
          self.assertEqual(ctx.get(operation.result), value, "saved response")
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
    raw, port = arb.clients
    start = Signal()
    finished = Signal()
    add_flow(m, [
      Step("start", comb=[], sync=[], until=start),
      StackPush("waiting_push", port, 0xC3),
      Sync("finished", finished.eq(1)),
    ])

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
    raw, port = arb.clients
    operation = StackPop("waiting_pop", port)
    self.check_result_shape(operation, stack)
    captured = Signal(8)
    finished = Signal()
    add_flow(m, [
      operation,
      Sync("capture", [captured.eq(operation.result), finished.eq(1)]),
    ], use_expand=True)

    async def bench(ctx):
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
      self.assertEqual(ctx.get(operation.result), 0xD6)
      self.assertEqual(ctx.get(port.req_pop), 0)
      await ctx.tick()
      self.assertEqual(ctx.get(finished), 1)
      self.assertEqual(ctx.get(captured), 0xD6)
      # Change both RAM's output and arbiter response ownership after saving.
      await self.raw_push(ctx, raw, 0x39)
      await self.raw_pop(ctx, raw, 0x39)
      for _ in range(5):
        self.assertEqual(ctx.get(operation.result), 0xD6)
        self.assertEqual(ctx.get(captured), 0xD6)
        self.assertEqual(ctx.get(stack.top), 0)
        self.assertEqual(ctx.get(port.req_push | port.req_pop), 0)
        await ctx.tick()

    self.simulate(m, bench)


if __name__ == "__main__":
  unittest.main(verbosity=2)
