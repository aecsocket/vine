"""Registry and value-copy policy: python -m ivga.slop.test_extrinsic_registry.

Use tiny mocked linker clients to isolate copy policy and skewed completions.
Fault outputs are levels; a test supervisor latches them without stopping cleanup.
"""

import random
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from amaranth import Const, Module, Signal
from amaranth.sim import Simulator
from amflow import expand

import ivga.extrinsic as registry
from ivga import Port, Tag
from ivga.extrinsic.common import ExtFn, ExtTy
from ivga.runtime.fault import FaultPort
from ivga.runtime.interact import Interact, copy_ext_val
from ivga.runtime.linker import LinkerPort

PERIOD = 1e-6


def packed(tag, payload=0, label=0):
  return Const({"tag": tag, "payload": payload, "label": label}, Port).as_value().value


class ExtrinsicRegistryTests(unittest.TestCase):
  def test_names_ids_and_signatures(self):
    type_names = [entry.name for entry in registry.EXT_TYS]
    function_names = [entry.name for entry in registry.EXT_FNS]
    self.assertEqual(len(type_names), len(set(type_names)))
    self.assertEqual(len(function_names), len(set(function_names)))
    self.assertEqual(registry.TY_IDS, {name: i for i, name in enumerate(type_names)})
    self.assertTrue({"ivm:n32", "ivm:branch"} <= set(type_names))
    self.assertTrue({"vi:n32:add", "vi:n32:sub", "vi:n32:eq", "vi:n32:ne", "ivm:branch"} <= set(function_names))
    for entry in registry.EXT_FNS:
      with self.subTest(function=entry.name):
        self.assertEqual(len(entry.inputs), 2, "current call machinery supports merge functions only")
        self.assertTrue(set(entry.inputs) <= set(type_names))
        self.assertTrue(callable(entry.fn))
    branch = next(entry for entry in registry.EXT_FNS if entry.name == "ivm:branch")
    self.assertEqual(branch.inputs, ("ivm:n32", "ivm:branch"))

  def test_function_label_encoding(self):
    self.assertEqual(registry.SWAP_BIT, 0x8000)
    self.assertEqual(registry.FN_ID_MASK, 0x7FFF)
    self.assertLessEqual(len(registry.EXT_FNS), registry.SWAP_BIT)
    for function_id in range(len(registry.EXT_FNS)):
      for swapped in (False, True):
        label = function_id | (registry.SWAP_BIT if swapped else 0)
        self.assertEqual(label & registry.FN_ID_MASK, function_id)
        self.assertEqual(bool(label & registry.SWAP_BIT), swapped)
        self.assertEqual(label ^ registry.SWAP_BIT, function_id | (0 if swapped else registry.SWAP_BIT))

  def test_input_signature_is_preserved(self):
    def handler(_cx):
      yield

    entry = ExtFn("test", inputs=("ivm:n32", "ivm:branch"), fn=handler)
    self.assertEqual(entry.inputs, ("ivm:n32", "ivm:branch"))
    self.assertEqual(tuple(entry.inputs), entry.inputs)
    self.assertIs(entry.fn, handler)

  def check_copy_lookup(self, types, mask):
    m = Module()
    label, result = Signal(16), Signal()
    with patch.object(registry, "EXT_TYS", types), patch.object(registry, "COPY_MASK", mask):
      m.d.comb += result.eq(registry.ext_val_label_is_copy(label))

    rng = random.Random(17)
    labels = list(range(len(types) + 4)) + [1 << bit for bit in range(16)]
    labels += [0x7FFF, 0x8000, 0xFFFF] + [rng.randrange(1 << 16) for _ in range(100)]

    async def bench(ctx):
      for value in labels:
        ctx.set(label, value)
        expected = value < len(types) and types[value].is_copy
        self.assertEqual(ctx.get(result), expected, f"copyability of type label {value}")

    sim = Simulator(m)
    sim.add_testbench(bench)
    sim.run()

  def test_copy_lookup_matches_registry(self):
    expected_mask = sum(int(entry.is_copy) << i for i, entry in enumerate(registry.EXT_TYS))
    self.assertEqual(registry.COPY_MASK, expected_mask)
    self.check_copy_lookup(registry.EXT_TYS, registry.COPY_MASK)

  def test_copy_lookup_mixed_types_and_unknown_labels(self):
    # All current builtins are copyable; this synthetic registry exercises holes
    # and ensures large unknown labels cannot alias a low copyable index.
    types = [ExtTy(f"test:{i}", is_copy=copy) for i, copy in enumerate((False, True, False, True, True, False))]
    self.check_copy_lookup(types, 0b011010)

  def check_copy_flow(self, label, copyable, grant_after, latency):
    m = Module()
    ports = [LinkerPort().create() for _ in range(2)]
    value, node = Signal(Port), Signal(Port)
    fault = FaultPort().create()
    done, seen_fault = Signal(), Signal()
    stat_copy = Signal(32)
    dut = SimpleNamespace(link_ports=ports, fault=fault, done=done, stat_copy=stat_copy)
    m.d.sync += done.eq(False)
    with m.If(fault.ext_copy):
      m.d.sync += seen_fault.eq(True)

    def program(f):
      yield from copy_ext_val(f, cast(Interact, cast(object, dut)), value, node)
      f.d.until += Const(0)
      yield

    types = [ExtTy("test:copyable", is_copy=True), ExtTy("test:linear", is_copy=False)]
    with patch.object(registry, "EXT_TYS", types), patch.object(registry, "COPY_MASK", 1):
      expand(m, program)

    async def bench(ctx):
      source = packed(Tag.EXT_VAL, 0x1234_5678, label)
      ctx.set(value.as_value(), source)
      ctx.set(node.as_value(), packed(Tag.COMB, 128, 7))
      accepted = [False, False]
      due: list[int | None] = [None, None]
      requests: list[tuple[int, int] | None] = [None, None]
      completions = 0
      for cycle in range(40):
        for i, port in enumerate(ports):
          ready = cycle >= grant_after[i]
          ctx.set(port.granted, ready)
          ctx.set(port.done, cycle == due[i])
          if ctx.get(port.req_link):
            request = (ctx.get(port.req_a.as_value()), ctx.get(port.req_b.as_value()))
            expected = (packed(Tag.WIRE, 128 + i), source if copyable else packed(Tag.ERASE))
            self.assertEqual(request, expected)
            if requests[i] is not None:
              self.assertEqual(request, requests[i], "held request changed before grant")
            requests[i] = request
            if ready:
              self.assertFalse(accepted[i], "duplicate link acceptance")
              accepted[i] = True
              due[i] = cycle + latency[i]
        self.assertEqual(ctx.get(fault.ext_copy), not copyable)
        await ctx.tick()
        if ctx.get(done):
          completions += 1
          self.assertTrue(all(accepted))
          self.assertGreaterEqual(cycle, max(deadline for deadline in due if deadline is not None))
          break
      else:
        self.fail("copy operation did not complete within 40 cycles")

      self.assertEqual(ctx.get(stat_copy), 1)
      self.assertEqual(ctx.get(seen_fault), not copyable)
      self.assertEqual(ctx.get(fault.ext_copy), 0, "fault output unexpectedly sticky")
      for _ in range(4):
        for port in ports:
          ctx.set(port.done, 0)
        await ctx.tick()
        completions += ctx.get(done)
        self.assertFalse(any(ctx.get(port.req_link) for port in ports))
      self.assertEqual(completions, 1)
      self.assertEqual(ctx.get(stat_copy), 1)
      self.assertEqual(ctx.get(seen_fault), not copyable, "supervisor lost the fault")

    sim = Simulator(m)
    sim.add_clock(PERIOD)
    sim.add_testbench(bench)
    sim.run()

  def test_copy_policy_routes_values_and_latches_faults(self):
    for label, copyable in ((0, True), (1, False), (2, False), (0xFFFF, False)):
      for grant_after, latency in (((0, 0), (1, 3)), ((0, 3), (8, 1))):
        with self.subTest(label=label, grant_after=grant_after, latency=latency):
          self.check_copy_flow(label, copyable, grant_after, latency)


if __name__ == "__main__":
  _ = unittest.main(verbosity=2)
