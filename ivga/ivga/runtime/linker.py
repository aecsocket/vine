from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from ivga.mem.alloc import AllocPort
from ivga.mem.heap import HeapPort

from ivga import Port, Tag, flatten


class LinkerPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_link": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "done": In(1),
    })

IDLE = "idle"
DO_LINK = "do_link"
FOLLOW = "follow"
CONNECT = "connect"
CONNECT_FREE = "connect_free"
CONNECT_SWAP = "connect_swap"
CONNECT_SWAP_FREE = "connect_swap_free"
FOLLOW_FREE = "follow_free"


class Linker(wiring.Component):
  control: In(LinkerPort())
  heap: Out(HeapPort())
  alloc: Out(AllocPort())

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
    self.swap_free_prev = Signal(Port)
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

def idle(linker: Linker, m: Module):
  with m.If(linker.req_link):
    m.d.sync += [
      linker.a.eq(linker.req_a),
      linker.b.eq(linker.req_b),
    ]
    do_link_(linker, m, linker.req_a, linker.req_b)

def do_link_(linker: Linker, m: Module, a, b):
  with m.If(a.tag == Tag.WIRE):
    with m.If(b.tag == Tag.WIRE):
      # A and B are wires; follow B to the end
      # then link A and the tail of B
      HeapPort.read(m, linker.heap, b.payload)
      m.next = FOLLOW
    with m.Else():
      # A is a wire, but B isn't, so we can go straight to connecting
      connect_(linker, m, a, b)

  with m.Elif(b.tag == Tag.WIRE):
    # A must not be a wire; go straight to connecting
    m.d.sync += [
      linker.a.eq(b),
      linker.b.eq(a),
    ]
    connect_(linker, m, b, a)

  with m.Elif(
    ((a.tag == Tag.GRAFT) | (a.tag == Tag.ERASE))
    & ((b.tag == Tag.GRAFT) | (b.tag == Tag.ERASE))
  ):
    # (graft|erase) >-< (graft|erase) erases each other;
    # nothing to do, since a graft is a closed net where the only port open
    # to the outside world *is* the one we're looking at right now.
    # so there's no other way for the graft to interact with the rest of the net
    # therefore it's safe to erase it
    m.d.sync += linker.done.eq(True)
    m.next = IDLE

  # TODO other cases
  with m.Else():
    m.d.sync += Assert(False, "all tag combinations should be handled")

def follow(linker: Linker, m: Module):
  HeapPort.read(m, linker.heap, linker.b.payload)
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
        connect_same_wire(linker, m, linker.a, linker.b)
      with m.Else():
        m.next = CONNECT
    with m.Else():
      # heap[B.payload] has a port, so we
      # - free B.payload, wait for it to be freed, then
      # - continue `link(a, heap[B.payload])`
      #   where `previous = heap[B.payload]`
      AllocPort.free(m, linker.alloc, linker.b.payload)
      m.d.sync += linker.previous.eq(target)
      m.next = FOLLOW_FREE

def connect_same_wire(linker: Linker, m: Module, a, b):
  # A's bits compose a wire, and B has the same bits,
  # so B is the same wire as A.
  # so we just free `A.payload`, and don't do any real connecting.
  AllocPort.free(m, linker.alloc, a.payload)
  m.next = CONNECT_FREE


def connect_(linker: Linker, m: Module, a, b):
  # we're done following wires
  with m.If(a == b):
    connect_same_wire(linker, m, a, b)
  with m.Else():
    # swap_target(A as Wire, B)
    # => swap(&mut heap[A.payload], B)
    HeapPort.read(m, linker.heap, a.payload)
    HeapPort.write(m, linker.heap, a.payload, b)
    m.next = CONNECT_SWAP

def connect_free(linker: Linker, m: Module):
  AllocPort.free(m, linker.alloc, linker.a.payload)
  with m.If(linker.alloc.done):
    m.d.sync += linker.done.eq(True)
    m.next = IDLE

def connect_swap(linker: Linker, m: Module):
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
      m.d.sync += linker.swap_free_prev.eq(previous)
      AllocPort.free(m, linker.alloc, linker.a.payload)
      m.next = CONNECT_SWAP_FREE

def connect_swap_free(linker: Linker, m: Module):
  AllocPort.free(m, linker.alloc, linker.a.payload)
  with m.If(linker.alloc.done):
    # A.payload is freed, do `link(previous, b)`
    m.d.sync += linker.a.eq(linker.swap_free_prev)
    do_link_(linker, m, linker.swap_free_prev, linker.b)

def follow_free(linker: Linker, m: Module):
  AllocPort.free(m, linker.alloc, linker.b.payload)
  with m.If(linker.alloc.done):
    # old intermediate wire has been freed,
    # continue `link(a, previous)`
    m.d.sync += linker.b.eq(linker.previous)
    do_link_(linker, m, linker.a, linker.previous)
