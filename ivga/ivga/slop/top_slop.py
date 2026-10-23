"""Optional interactive Arty A7 harness with runtime hardware statistics.

BTN0 starts; BTN1 resets/rearms; BTN2 halts; BTN3 sends a statistics snapshot.
LED0 latches completion; LED1 latches fault/halt. SW0 selects empty/eraser
auxiliaries, SW1 injects mismatched COMB labels (timeout), SW2..3 select banks
of four heap words. Controls are debounced; switches are captured on Start.

Each Start resets the test core/counters and reloads memory. Allocator counts
include setup allocations; peak is the allocator's bump-pointer watermark.
The USB-UART (115200, 8N1) accepts '?' for an atomic ivga1 snapshot. Readout
stays operational while the test runs or is halted. No worklist reducer is
implemented: Done describes one interaction, not an entire net reduction.
"""

from amaranth import *
from amaranth.lib import io, wiring
from amaranth.lib.cdc import FFSynchronizer
from amaranth.lib.wiring import In, Out

from ivga import ERASER, PORT_EMPTY, Port, Port2, Tag
from ivga.mem.alloc import Alloc
from ivga.mem.heap import make_heap
from ivga.mem.stack import Stack
from ivga.runtime.interact import Interact
from ivga.runtime.link import Link
from ivga.slop.stats_uart import StatsUART


class Arbiter(wiring.Component):
  request_a: In(1)
  request_b: In(1)
  grant_a: Out(1)
  grant_b: Out(1)

  def __init__(self):
    self.current = Signal()
    super().__init__()

  def elaborate(self, platform):
    m = Module()
    m.d.comb += [
      self.grant_a.eq(self.request_a & (~self.request_b | ~self.current)),
      self.grant_b.eq(self.request_b & (~self.request_a | self.current)),
    ]
    with m.If(self.grant_a | self.grant_b):
      m.d.sync += self.current.eq(self.grant_a)
    return m


def button_press(m, raw, cycles, name):
  synchronized = Signal(name=f"{name}_synchronized")
  stable = Signal(name=f"{name}_stable")
  count = Signal(range(cycles), name=f"{name}_count")
  pressed = Signal(name=f"{name}_pressed")
  m.submodules[f"{name}_synchronizer"] = FFSynchronizer(raw, synchronized)
  m.d.comb += pressed.eq(synchronized & ~stable & (count == cycles - 1))
  with m.If(synchronized == stable):
    m.d.sync += count.eq(0)
  with m.Elif(count == cycles - 1):
    m.d.sync += [stable.eq(synchronized), count.eq(0)]
  with m.Else():
    m.d.sync += count.eq(count + 1)
  return pressed


class Processor(wiring.Component):
  def __init__(self, *, debounce_cycles=500_000, timeout_cycles=100_000, uart_divisor=868):
    assert debounce_cycles > 0 and timeout_cycles > 0
    self.debounce_cycles = debounce_cycles
    self.timeout_cycles = timeout_cycles
    self.uart_divisor = uart_divisor
    self.start_button = Signal()
    self.reset_button = Signal()
    self.halt_button = Signal()
    self.read_button = Signal()
    self.switches = Signal(4)
    self.selection = Signal(4)
    self.done = Signal()
    self.halted = Signal()
    self.busy = Signal()
    self.timed_out = Signal()
    self.uart_rx = Signal(init=1)
    self.uart_tx = Signal(init=1)

    self.heap = make_heap()
    self.alloc = Alloc(capacity=512)
    self.link = Link()
    self.interact = Interact()
    self.active = Stack(Port2, depth=16)
    self.stat_allocs = self.alloc.stat_allocs
    self.stat_frees = self.alloc.stat_frees
    self.stat_peak = self.alloc.stat_peak
    self.stat_erase = self.link.stat_erase
    self.stat_annihilate = self.interact.stat_annihilate
    super().__init__({})

  def elaborate(self, platform):
    m = Module()
    if platform is not None:
      buttons = (self.start_button, self.reset_button, self.halt_button, self.read_button)
      for index, signal in enumerate(buttons):
        buffer = io.Buffer("i", platform.request("button", index, dir="-"))
        m.submodules[f"button_{index}"] = buffer
        m.d.comb += signal.eq(buffer.i)
      for index in range(4):
        buffer = io.Buffer("i", platform.request("switch", index, dir="-"))
        m.submodules[f"switch_{index}"] = buffer
        m.d.comb += self.switches[index].eq(buffer.i)
      for index, signal in enumerate((self.done, self.halted)):
        buffer = io.Buffer("o", platform.request("led", index, dir="-"))
        m.submodules[f"led_{index}"] = buffer
        m.d.comb += buffer.o.eq(signal)
      pins = platform.request("uart", 0, dir={"rx": "-", "tx": "-"})
      m.submodules.uart_rx = rx_buffer = io.Buffer("i", pins.rx)
      m.submodules.uart_tx = tx_buffer = io.Buffer("o", pins.tx)
      m.d.comb += [self.uart_rx.eq(rx_buffer.i), tx_buffer.o.eq(self.uart_tx)]

    start = button_press(m, self.start_button, self.debounce_cycles, "start")
    reset = button_press(m, self.reset_button, self.debounce_cycles, "reset")
    halt = button_press(m, self.halt_button, self.debounce_cycles, "halt")
    read = button_press(m, self.read_button, self.debounce_cycles, "read")
    switches = Signal(4)
    m.submodules.switch_synchronizer = FFSynchronizer(self.switches, switches)
    accepted_start = start & ~self.busy & ~self.halted & ~reset & ~halt
    runtime_reset = reset | accepted_start

    core = Module()
    # Keep the UART and controls outside this reset/enable boundary. A faulted
    # core must remain inspectable, and reset must work even while it is halted.
    m.submodules.core = ResetInserter(runtime_reset)(EnableInserter(~self.halted)(core))
    core.submodules.heap = heap = self.heap
    heap_read = heap.read_port()
    heap_write = heap.write_port()
    core.submodules.heap_arb = arbiter = Arbiter()
    core.submodules.alloc = alloc = self.alloc
    core.submodules.link = link = self.link
    core.submodules.interact = interact = self.interact
    core.submodules.active = active = self.active
    wiring.connect(core, link.active, active.control)

    running = Signal()
    read_enable = Signal()
    write_enable = Signal()
    pending = Signal()
    owner = Signal()
    init_addr = Signal(range(heap.depth))
    reserved = Signal(range(17))
    watchdog = Signal(range(self.timeout_cycles))
    base = Signal(4)
    a, b = Signal(Port), Signal(Port)
    core.d.comb += [
      base.eq(Cat(Const(0, 2), self.selection[2:4])),
      a.tag.eq(Tag.COMB), a.payload.eq(base), a.label.eq(0),
      b.tag.eq(Tag.COMB), b.payload.eq(base + 2), b.label.eq(self.selection[1]),
      interact.req_a.eq(a), interact.req_b.eq(b),
      link.req_a.eq(interact.link.req_a), link.req_b.eq(interact.link.req_b),
      # Callers hold requests through acknowledgement; the newly idle callee
      # must not accept them again on that same completion cycle.
      link.req_link.eq(running & interact.link.req_link & ~link.done),
      interact.link.done.eq(running & link.done),
      alloc.req_free.eq(running & link.alloc.req_free & ~alloc.done),
      alloc.free_addr.eq(link.alloc.free_addr),
      link.alloc.done.eq(running & alloc.done),
      link.alloc.oom.eq(alloc.oom),
      link.alloc.alloc_addr.eq(alloc.alloc_addr),
      link.alloc.stat_allocs.eq(alloc.stat_allocs),
      link.alloc.stat_frees.eq(alloc.stat_frees),
      link.alloc.stat_peak.eq(alloc.stat_peak),
      interact.link.stat_erase.eq(link.stat_erase),
      # Core reset clears all statistics, including modules whose stat_reset
      # input is currently only declared and not yet implemented.
      alloc.stat_reset.eq(False), link.stat_reset.eq(False), interact.stat_reset.eq(False),
      link.fault.halt.eq(self.halted),
      heap_read.en.eq(read_enable & ~self.halted & ~runtime_reset),
      heap_write.en.eq(write_enable & ~self.halted & ~runtime_reset),
      alloc.heap.read_data.eq(heap_read.data), link.heap.read_data.eq(heap_read.data),
      alloc.heap.done.eq(running & pending & ~owner & ~self.halted),
      link.heap.done.eq(running & pending & owner & ~self.halted),
      arbiter.request_a.eq(running & ~pending & (alloc.heap.read | alloc.heap.write)),
      arbiter.request_b.eq(running & ~pending & (link.heap.read | link.heap.write)),
    ]

    # Issue one physical memory access, then acknowledge the captured owner on
    # the next cycle. Simultaneous read/write returns the pre-write word.
    with core.If(pending):
      core.d.sync += pending.eq(False)
    for grant, client, index in ((arbiter.grant_a, alloc.heap, 0),
                                 (arbiter.grant_b, link.heap, 1)):
      with core.If(running & ~pending & grant):
        core.d.comb += [
          read_enable.eq(client.read), heap_read.addr.eq(client.addr),
          write_enable.eq(client.write), heap_write.addr.eq(client.addr),
          heap_write.data.eq(client.write_data),
        ]
        core.d.sync += [pending.eq(True), owner.eq(index)]

    with core.FSM(init="idle"):
      with core.State("idle"), core.If(self.busy):
        core.next = "clear"
      with core.State("clear"):
        core.d.comb += [
          write_enable.eq(True), heap_write.addr.eq(init_addr),
          heap_write.data.eq(Mux(
            self.selection[0] & (init_addr >= base) & (init_addr < base + 4),
            Const(ERASER, Port).as_value(), PORT_EMPTY.as_value(),
          )),
        ]
        with core.If(init_addr == heap.depth - 1):
          core.next = "reserve"
        with core.Else():
          core.d.sync += init_addr.eq(init_addr + 1)
      with core.State("reserve"):
        # Reserve all addresses up to the selected bank before any frees.
        with core.If(reserved < base + 4):
          core.d.comb += alloc.req_alloc.eq(True)
          core.next = "reserve_wait"
        with core.Else():
          core.next = "start"
      with core.State("reserve_wait"), core.If(alloc.done):
        core.d.sync += reserved.eq(reserved + 1)
        core.next = "reserve"
      with core.State("start"):
        core.d.comb += [running.eq(True), interact.req_interact.eq(True)]
        core.next = "run"
      with core.State("run"):
        core.d.comb += running.eq(True)
        with core.If(interact.done):
          core.next = "idle"

    with core.If(running & (watchdog < self.timeout_cycles - 1)):
      core.d.sync += watchdog.eq(watchdog + 1)
    timeout = running & (watchdog == self.timeout_cycles - 1)
    fault = halt | (self.busy & (alloc.oom | link.fault.active_full | timeout))
    with m.If(reset):
      m.d.sync += [self.done.eq(False), self.halted.eq(False),
                   self.busy.eq(False), self.timed_out.eq(False)]
    with m.Elif(fault):
      m.d.sync += [self.halted.eq(True), self.busy.eq(False), self.timed_out.eq(timeout)]
    with m.Elif(accepted_start):
      m.d.sync += [self.selection.eq(switches), self.done.eq(False),
                   self.busy.eq(True), self.timed_out.eq(False)]
    with m.Elif(running & interact.done):
      m.d.sync += [self.done.eq(True), self.busy.eq(False)]

    status = Cat(self.busy, self.done, self.halted, self.timed_out)
    m.submodules.stats = self.stats = StatsUART((
      status, self.selection, self.stat_allocs, self.stat_frees, self.stat_peak,
      self.stat_erase, self.stat_annihilate,
    ), divisor=self.uart_divisor)
    m.d.comb += [
      self.stats.rx.eq(self.uart_rx), self.uart_tx.eq(self.stats.tx),
      self.stats.request.eq(read),
    ]
    return m
