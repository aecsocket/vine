from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out

from ivga import Port, Port2, Tag, flatten
from ivga.mem import AllocPort, HeapPort, StackPort

from .fault import FaultPort


class LinkPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_link": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "done": In(1),
    })

  @staticmethod
  def link(m: Module, link, a, b):
    m.d.comb += [
      link.req_link.eq(True),
      link.req_a.eq(a),
      link.req_b.eq(b),
    ]

IDLE = "idle"
DO_LINK = "do_link"
FOLLOW = "follow"
CONNECT = "connect"
CONNECT_FREE = "connect_free"
CONNECT_SWAP = "connect_swap"
CONNECT_SWAP_FREE = "connect_swap_free"
FOLLOW_FREE = "follow_free"


class Link(wiring.Component):
  control: In(LinkPort())
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
        idle(self, m)
      with m.State(DO_LINK):
        do_link_(self, m, self.a, self.b)
      with m.State(FOLLOW):
        follow(self, m)
      with m.State(CONNECT):
        connect_(self, m, self.a, self.b)
      with m.State(CONNECT_FREE):
        connect_free(self, m)
      with m.State(CONNECT_SWAP):
        connect_swap(self, m)
      with m.State(CONNECT_SWAP_FREE):
        connect_swap_free(self, m)
      with m.State(FOLLOW_FREE):
        follow_free(self, m)

    return m

def idle(link: Link, m: Module):
  with m.If(link.req_link):
    m.d.sync += [
      link.a.eq(link.req_a),
      link.b.eq(link.req_b),
    ]
    do_link_(link, m, link.req_a, link.req_b)

def do_link_(link: Link, m: Module, a, b):
  with m.If(a.tag == Tag.WIRE):
    with m.If(b.tag == Tag.WIRE):
      # A and B are wires; follow B to the end
      # then link A and the tail of B
      HeapPort.read(m, link.heap, b.payload)
      m.next = FOLLOW
    with m.Else():
      # A is a wire, but B isn't, so we can go straight to connecting
      connect_(link, m, a, b)

  with m.Elif(b.tag == Tag.WIRE):
    # A must not be a wire; go straight to connecting
    m.d.sync += [
      link.a.eq(b),
      link.b.eq(a),
    ]
    connect_(link, m, b, a)

  with m.Elif(
    ((a.tag == Tag.GRAFT) | (a.tag == Tag.ERASE))
    & ((b.tag == Tag.GRAFT) | (b.tag == Tag.ERASE))
  ):
    # (graft|erase) >-< (graft|erase) erases each other;
    # nothing to do, since a graft is a closed net where the only port open
    # to the outside world *is* the one we're looking at right now.
    # so there's no other way for the graft to interact with the rest of the net
    # therefore it's safe to erase it
    m.d.sync += link.done.eq(True)
    m.next = IDLE

  with m.Elif(
    ((a.tag == Tag.EXT_VAL) | (a.tag == Tag.ERASE))
    & ((b.tag == Tag.EXT_VAL) | (b.tag == Tag.ERASE))
  ):
    # (extval|erase) >-< (extval|erase) - erasing an ext val is legal,
    # but might be a bad idea (e.g. erasing the IO handle).
    m.d.sync += link.done.eq(True)
    m.next = IDLE

  with m.Else():
    # all other work goes on the worklist
    # TODO: active_fast/active_slow like in IVM
    with m.If(link.active.full):
      # fault if active worklist is full
      m.d.comb += link.fault.active_full.eq(True)
    with m.Else():
      pair = Signal(Port2)
      m.d.comb += [
        pair.a.eq(a),
        pair.b.eq(b),
      ]
      StackPort.push(m, link.active, pair)
      m.d.sync += link.done.eq(True)
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

def follow(link: Link, m: Module):
  HeapPort.read(m, link.heap, link.b.payload)
  with m.If(link.heap.done):
    # fetched heap[B.payload]
    target = Port(link.heap.read_data)
    with m.If(target.tag == Tag.INVALID):
      # heap[B.payload] is empty, so B is the end of the chain;
      # skip to connecting.
      #
      # reimpl a bit of `connect_` manually, because
      # we can't instantiate another `connect_`,
      # since both follow and connect steps use the heap.
      # so we may have to wait for another clock cycle.
      with m.If(link.a == link.b):
        connect_same_wire(link, m, link.a, link.b)
      with m.Else():
        m.next = CONNECT
    with m.Else():
      # heap[B.payload] has a port, so we
      # - free B.payload, wait for it to be freed, then
      # - continue `link(a, heap[B.payload])`
      #   where `previous = heap[B.payload]`
      AllocPort.free(m, link.alloc, link.b.payload)
      m.d.sync += link.previous.eq(target)
      m.next = FOLLOW_FREE

def connect_same_wire(link: Link, m: Module, a, b):
  # A's bits compose a wire, and B has the same bits,
  # so B is the same wire as A.
  # so we just free `A.payload`, and don't do any real connecting.
  AllocPort.free(m, link.alloc, a.payload)
  m.next = CONNECT_FREE


def connect_(link: Link, m: Module, a, b):
  # we're done following wires
  with m.If(a == b):
    connect_same_wire(link, m, a, b)
  with m.Else():
    # swap_target(A as Wire, B)
    # => swap(&mut heap[A.payload], B)
    HeapPort.read(m, link.heap, a.payload)
    HeapPort.write(m, link.heap, a.payload, b)
    m.next = CONNECT_SWAP

def connect_free(link: Link, m: Module):
  AllocPort.free(m, link.alloc, link.a.payload)
  with m.If(link.alloc.done):
    m.d.sync += link.done.eq(True)
    m.next = IDLE

def connect_swap(link: Link, m: Module):
  HeapPort.read(m, link.heap, link.a.payload)
  HeapPort.write(m, link.heap, link.a.payload, link.b)
  with m.If(link.heap.done):
    # swap complete, `previous = heap[A.payload]`
    previous = Port(link.heap.read_data)
    with m.If(previous.tag == Tag.INVALID):
      # no node at A.payload's old slot, no work left
      m.d.sync += link.done.eq(True)
      m.next = IDLE
    with m.Else():
      # free A.payload and `link(previous, b)`
      # in this cycle we only free A.payload
      # save `previous` into a register
      # cause we need to set `a = previous`
      m.d.sync += link.previous.eq(previous)
      AllocPort.free(m, link.alloc, link.a.payload)
      m.next = CONNECT_SWAP_FREE

def connect_swap_free(link: Link, m: Module):
  AllocPort.free(m, link.alloc, link.a.payload)
  with m.If(link.alloc.done):
    # A.payload is freed, do `link(previous, b)`
    m.d.sync += link.a.eq(link.previous)
    do_link_(link, m, link.previous, link.b)

def follow_free(link: Link, m: Module):
  AllocPort.free(m, link.alloc, link.b.payload)
  with m.If(link.alloc.done):
    # old intermediate wire has been freed,
    # continue `link(a, previous)`
    m.d.sync += link.b.eq(link.previous)
    do_link_(link, m, link.a, link.previous)
