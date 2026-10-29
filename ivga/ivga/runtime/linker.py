from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from ivga.mem.alloc import AllocPort
from ivga.mem.heap import HeapPort
from ivga.mem.stack import StackPort

from ivga import Port, Port2, Stat, Tag, flatten

from .fault import FaultPort


class LinkerPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_link": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "granted": In(1),
      "done": In(1),
      # stats
      "stat_erase": In(Stat),
      "stat_reset": Out(1),
    })

  @staticmethod
  def link(m: Module, linker, a, b):
    m.d.comb += [
      linker.req_link.eq(True),
      linker.req_a.eq(a),
      linker.req_b.eq(b),
    ]

IDLE = "idle"
DO_LINK = "do_link"
FOLLOW_REQ = "follow_req"
FOLLOW_WAIT = "follow_wait"
CONNECT = "connect"
CONNECT_FREE = "connect_free"
CONNECT_SWAP = "connect_swap"
CONNECT_SWAP_FREE = "connect_swap_free"
FOLLOW_FREE = "follow_free"


class Linker(wiring.Component):
  control: In(LinkerPort())
  heap: Out(HeapPort())
  alloc: Out(AllocPort())
  active: Out(StackPort(Port2))
  fault: Out(FaultPort())

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
    self.previous = Signal(Port)
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()
    m.d.sync += self.done.eq(False)

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(m, self)
      with m.State(DO_LINK):
        do_link(m, self, self.a, self.b)
      with m.State(FOLLOW_REQ):
        follow_req(m, self, self.b.payload)
      with m.State(FOLLOW_WAIT):
        follow_wait(m, self)
      with m.State(CONNECT):
        connect(m, self, self.a, self.b)
      with m.State(CONNECT_FREE):
        connect_free(m, self)
      with m.State(CONNECT_SWAP):
        connect_swap(m, self)
      with m.State(CONNECT_SWAP_FREE):
        connect_swap_free(m, self)
      with m.State(FOLLOW_FREE):
        follow_free(m, self)

    return m

def idle(m: Module, linker: Linker):
  with m.If(linker.req_link):
    m.d.sync += [
      linker.a.eq(linker.req_a),
      linker.b.eq(linker.req_b),
    ]
    do_link(m, linker, linker.req_a, linker.req_b)

def do_link(m: Module, linker: Linker, a, b):
  with m.If(a.tag == Tag.WIRE):
    with m.If(b.tag == Tag.WIRE):
      # A and B are wires; follow B to the end
      # then link A and the tail of B
      follow_req(m, linker, b.payload)
    with m.Else():
      # A is a wire, but B isn't, so we can go straight to connecting
      connect(m, linker, a, b)

  with m.Elif(b.tag == Tag.WIRE):
    # A must not be a wire; go straight to connecting
    m.d.sync += [
      linker.a.eq(b),
      linker.b.eq(a),
    ]
    connect(m, linker, b, a)

  with m.Elif(
    ((a.tag == Tag.GRAFT) | (a.tag == Tag.ERASE))
    & ((b.tag == Tag.GRAFT) | (b.tag == Tag.ERASE))
  ):
    # (graft|erase) >-< (graft|erase) erases each other;
    # nothing to do, since a graft is a closed net where the only port open
    # to the outside world *is* the one we're looking at right now.
    # so there's no other way for the graft to interact with the rest of the net
    # therefore it's safe to erase it
    m.d.sync += [
      linker.done.eq(True),
      linker.stat_erase.eq(linker.stat_erase + 1),
    ]
    m.next = IDLE

  with m.Elif(
    ((a.tag == Tag.EXT_VAL) | (a.tag == Tag.ERASE))
    & ((b.tag == Tag.EXT_VAL) | (b.tag == Tag.ERASE))
  ):
    # (extval|erase) >-< (extval|erase) - erasing an ext val is legal,
    # but might be a bad idea (e.g. erasing the IO handle).
    m.d.sync += [
      linker.done.eq(True),
      linker.stat_erase.eq(linker.stat_erase + 1),
    ]
    m.next = IDLE

  with m.Else():
    # all other work goes on the worklist
    # TODO: active_fast/active_slow like in IVM
    with m.If(linker.active.full):
      # fault if active worklist is full
      m.d.comb += linker.fault.active_full.eq(True)
    with m.Else():
      pair = Signal(Port2)
      m.d.comb += [
        pair.a.eq(a),
        pair.b.eq(b),
      ]
      StackPort.push(m, linker.active, pair)
      m.d.sync += linker.done.eq(True)
      m.next = IDLE

#   with m.Elif(
#     ((a.tag == Tag.COMB & b.tag == Tag.COMB) & a.label == b.label)
#     | ((a.tag == Tag.EXT_FN & b.tag == Tag.EXT_FN) & a.label == b.label)
#   ):
#     # N >-< N where nodes N have the same label
#
#
#   # TODO other cases
#   with m.Else():
#     m.d.sync += Assert(False, "all tag combinations should be handled")

def follow_req(m: Module, linker: Linker, b_payload):
  HeapPort.read(m, linker.heap, b_payload)
  with m.If(linker.heap.granted):
    m.next = FOLLOW_WAIT
  with m.Else():
    m.next = FOLLOW_REQ

def follow_wait(m: Module, linker: Linker):
  with m.If(linker.heap.done):
    # fetched heap[B.payload]
    target = Port(linker.heap.read_data)
    with m.If(target.tag == Tag.INVALID):
      # heap[B.payload] is empty, so B is the end of the chain;
      # skip to connecting.
      #
      # reimpl a bit of `connect_` manually, because
      # we can't instantiate another `connect_`,
      # since both follow and connect steps use the heap.
      # so we may have to wait for another clock cycle.
      with m.If(linker.a == linker.b):
        connect_same_wire(m, linker, linker.a, linker.b)
      with m.Else():
        m.next = CONNECT
    with m.Else():
      # heap[B.payload] has a port, so we
      # - free B.payload, wait for it to be freed, then
      # - continue `link(a, heap[B.payload])`
      #   where `previous = heap[B.payload]`
      AllocPort.free_one(m, linker.alloc, linker.b.payload)
      m.d.sync += linker.previous.eq(target)
      m.next = FOLLOW_FREE

def connect_same_wire(m: Module, linker: Linker, a, b):
  # A's bits compose a wire, and B has the same bits,
  # so B is the same wire as A.
  # so we just free `A.payload`, and don't do any real connecting.
  AllocPort.free_one(m, linker.alloc, a.payload)
  m.next = CONNECT_FREE


def connect(m: Module, linker: Linker, a, b):
  # we're done following wires
  with m.If(a == b):
    connect_same_wire(m, linker, a, b)
  with m.Else():
    # swap_target(A as Wire, B)
    # => swap(&mut heap[A.payload], B)
    HeapPort.swap(m, linker.heap, a.payload, b)
    m.next = CONNECT_SWAP

def connect_free(m: Module, linker: Linker):
  AllocPort.free_one(m, linker.alloc, linker.a.payload)
  with m.If(linker.alloc.done):
    m.d.sync += linker.done.eq(True)
    m.next = IDLE

def connect_swap(m: Module, linker: Linker):
  HeapPort.read(m, linker.heap, linker.a.payload)
  HeapPort.write(m, linker.heap, linker.a.payload, linker.b)
  with m.If(linker.heap.done):
    # swap complete, `previous = heap[A.payload]`
    previous = Port(linker.heap.read_data)
    with m.If(previous.tag == Tag.INVALID):
      # no node at A.payload's old slot, no work left
      m.d.sync += linker.done.eq(True)
      m.next = IDLE
    with m.Else():
      # free A.payload and `link(previous, b)`
      # in this cycle we only free A.payload
      # save `previous` into a register
      # cause we need to set `a = previous`
      m.d.sync += linker.previous.eq(previous)
      AllocPort.free_one(m, linker.alloc, linker.a.payload)
      m.next = CONNECT_SWAP_FREE

def connect_swap_free(m: Module, linker: Linker):
  AllocPort.free_one(m, linker.alloc, linker.a.payload)
  with m.If(linker.alloc.done):
    # A.payload is freed, do `link(previous, b)`
    m.d.sync += linker.a.eq(linker.previous)
    do_link(m, linker, linker.previous, linker.b)

def follow_free(m: Module, linker: Linker):
  AllocPort.free_one(m, linker.alloc, linker.b.payload)
  with m.If(linker.alloc.done):
    # old intermediate wire has been freed,
    # continue `link(a, previous)`
    m.d.sync += linker.b.eq(linker.previous)
    do_link(m, linker, linker.a, linker.previous)
