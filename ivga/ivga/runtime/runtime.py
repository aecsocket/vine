# SLOP
from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from amflow import Flow, expand
from ivga.mem.heap import CAPACITY, Heap, HeapArbiter, HeapPort
from ivga.mem.page_alloc import PAGE_SIZE, PageAlloc, PageAllocArbiter, PageAllocPort
from ivga.mem.pair_alloc import PairAlloc, PairAllocArbiter, PairAllocPort
from ivga.mem.stack import Stack, StackArbiter, StackPort

from ivga import Addr, Port, Port2

from .fault import FaultPort
from .grafter import Grafter, GrafterPort
from .interact import Interact, InteractPort
from .linker import Linker


class Runtime(wiring.Component):
  """Graft an entry net, then reduce its worklist one interaction at a time.

  Reserve the program's prefix pages with `reserved_pages` at construction,
  or allocate them through `pages` before pulsing `start`. Initialize the program
  words in BRAM or through `heap`, and any storage referenced by `root_port`.
  `pairs` is available for allocating that root storage. These host interfaces
  are disabled while busy; finish all host transactions before starting.

  `done` pulses after reduction or a fault, once the current operation completes.
  Faults and an external halt during execution are sticky and require reset
  before another execution can start. Construction is not rolled back on failure.
  `start` is edge-triggered; requests while busy are ignored.
  """

  start: In(1)
  entry_addr: In(Addr)
  root_port: In(Port)
  busy: Out(1)
  done: Out(1)
  failed: Out(1)

  halted: Out(1)
  fault: Out(FaultPort())

  heap: In(HeapPort())
  pages: In(PageAllocPort())
  pairs: In(PairAllocPort())

  def __init__(self, *, active_depth: int = 128, reserved_pages: int = 0):
    self.memory = Heap()
    self.page_alloc = PageAlloc(n_pages=CAPACITY // PAGE_SIZE, reserved_pages=reserved_pages)
    self.pair_alloc = PairAlloc()
    self.active = Stack(Port2, depth=active_depth)
    self.interact = Interact()
    self.entry_grafter = Grafter()
    self.entry_linker = Linker()
    self._was_start = Signal()
    super().__init__()

  def elaborate(self, platform):
    m, runtime = Module(), self
    for name in ("memory", "page_alloc", "pair_alloc", "active", "interact",
                 "entry_grafter", "entry_linker"):
      m.submodules[name] = getattr(self, name)

    heap_clients = (self.page_alloc.heap, self.pair_alloc.heap, *self.interact.heap,
                    self.entry_grafter.heap, self.entry_linker.heap)
    page_clients = (self.pair_alloc.pages, self.interact.pages, self.entry_grafter.pages)
    pair_clients = (*self.interact.pairs, self.entry_grafter.pairs, self.entry_linker.alloc)
    active_clients = (*self.interact.active, self.entry_linker.active)

    # Each resource has one arbitration level; the final client is the host or dispatcher.
    heap_arb = m.submodules.heap_arb = HeapArbiter(len(heap_clients) + 1)
    pages_arb = m.submodules.pages_arb = PageAllocArbiter(len(page_clients) + 1)
    pairs_arb = m.submodules.pairs_arb = PairAllocArbiter(len(pair_clients) + 1)
    active_arb = m.submodules.active_arb = StackArbiter(len(active_clients) + 1, Port2)

    for arb, clients, real in (
      (heap_arb, heap_clients, self.memory.control),
      (pages_arb, page_clients, self.page_alloc.control),
      (pairs_arb, pair_clients, self.pair_alloc.control),
      (active_arb, active_clients, self.active.control),
    ):
      for client, port in zip(clients, arb.clients[:-1], strict=True):
        wiring.connect(m, client, port)
      wiring.connect(m, arb.real, real)

    wiring.connect(m, self.entry_grafter.linker, self.entry_linker.control)
    self.worklist = active_arb.clients[-1]

    fault_sources = (self.interact, self.entry_linker, *self.interact.linkers)
    fault_bits = []
    for name, member in FaultPort().members.items():
      if member.flow != Out:
        continue
      latched = getattr(self.fault, name)
      events = Cat(*(getattr(source.fault, name) for source in fault_sources)).any()
      if name == "oom":
        events = events | (self.entry_grafter.done & self.entry_grafter.oom)
        events = events | (self.interact.grafter.done & self.interact.grafter.oom)
      elif name == "bad_graft":
        events = events | (self.entry_grafter.done & self.entry_grafter.error)
        events = events | (self.interact.grafter.done & self.interact.grafter.error)
      m.d.sync += latched.eq(latched | events)
      fault_bits.append(latched)
    m.d.sync += self.halted.eq(self.halted | (self.busy & self.fault.halt))
    m.d.comb += self.failed.eq(Cat(*fault_bits, self.halted).any())
    for source in fault_sources:
      m.d.comb += source.fault.halt.eq(self.fault.halt | self.failed)

    for host, client in ((self.heap, heap_arb.clients[-1]),
                         (self.pages, pages_arb.clients[-1]),
                         (self.pairs, pairs_arb.clients[-1])):
      with m.If(~self.busy & ~self.start):
        wiring.connect(m, wiring.flipped(host), client)

    m.d.sync += [
      self.done.eq(False),
      self._was_start.eq(self.start),
    ]
    expand(m, lambda f: program(f, runtime, self.start & ~self._was_start))
    return m


def program(f: Flow, runtime: Runtime, start: Value):
  with f.If(start & ~runtime.failed & ~runtime.fault.halt):
    addr = f.capture(runtime.entry_addr)
    root = f.capture(runtime.root_port)
    f.d.sync += runtime.busy.eq(True)
    yield
    yield GrafterPort.graft(f, runtime.entry_grafter, addr=addr, port=root)

    with f.While(~runtime.failed & ~runtime.fault.halt & ~runtime.worklist.empty):
      yield
      next_pair = yield StackPort.pop(f, runtime.worklist)
      yield
      yield InteractPort.interact(f, runtime.interact, a=next_pair.a, b=next_pair.b)

    f.d.sync += [
      runtime.busy.eq(False),
      runtime.done.eq(True),
    ]
    yield
