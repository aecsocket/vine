from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out
from ivga.mem.alloc import AllocPort
from ivga.mem.heap import HeapPort

from ivga import Port, Tag, flatten


class LinkPort(wiring.Signature):
  def __init__(self):
    super().__init__({
      "req_link": Out(1),
      "req_a": Out(Port),
      "req_b": Out(Port),
      "done": In(1),
    })

IDLE = "idle"
DO_LINK = "do_link"
FOLLOW_WAIT = "follow_wait"
FOLLOW_READ = "follow_read"
FOLLOW_WAIT_FREE = "follow_wait_free"
CONNECT = "connect"
CONNECT_WAIT_FREE = "connect_wait_free"
CONNECT_WAIT_WRITE = "connect_wait_write"
CONNECT_FINISH_LINK = "connect_finish_link"


class Link(wiring.Component):
  control: In(LinkPort())
  heap: Out(HeapPort())
  alloc: Out(AllocPort())

  def __init__(self):
    self.a = Signal(Port)
    self.b = Signal(Port)
    super().__init__()
    flatten(self, self.control)

  def elaborate(self, platform):
    m = Module()

    with m.FSM(init=IDLE):
      with m.State(IDLE):
        idle(self, m)
      with m.State(DO_LINK):
        do_link(self, m)
      with m.State(FOLLOW_WAIT):
        follow_wait(self, m)
      with m.State(FOLLOW_READ):
        follow_read(self, m)
      with m.State(FOLLOW_WAIT_FREE):
        follow_wait_free(self, m)
      with m.State(CONNECT):
        connect(self, m)
      with m.State(CONNECT_WAIT_FREE):
        connect_wait_free(self, m)
      with m.State(CONNECT_WAIT_WRITE):
        connect_wait_write(self, m)
      with m.State(CONNECT_FINISH_LINK):
        connect_finish_link(self, m)

    return m

def idle(link: Link, m: Module):
  with m.If(link.req_link):
    m.d.sync += [
      link.a.eq(link.req_a),
      link.b.eq(link.req_b),
    ]
    m.next = DO_LINK

def do_link(link: Link, m: Module):
  with m.If(link.a.tag == Tag.WIRE):
    with m.If(link.b.tag == Tag.WIRE):
      # B is also a wire, follow it to the end
      HeapPort.read(m, link.heap, link.b.payload)
      m.next = FOLLOW_WAIT
    with m.Else():
      # A is a wire but B isn't, just connect the 2
      m.next = CONNECT

  with m.Elif(link.b.tag == Tag.WIRE):
    # if we've gone in here, then we *know* that A can't be a wire,
    # so we just swap A and B, and continue connecting.
    # we swap because the CONNECT state assumes that A is the wire
    m.d.sync += [
      link.a.eq(link.b),
      link.b.eq(link.a),
    ]
    m.next = CONNECT

  with m.Elif(
    ((link.a.tag == Tag.GRAFT).bool() | (link.a.tag == Tag.ERASE).bool())
    & ((link.b.tag == Tag.GRAFT).bool() | (link.b.tag == Tag.ERASE).bool())
  ):
    # (graft|erase) >-< (graft|erase) erases each other;
    # nothing to do, since a graft is a closed net where the only port open
    # to the outside world *is* the one we're looking at right now.
    # so there's no other way for the graft to interact with the rest of the net
    # therefore it's safe to erase it
    m.d.sync += link.done.eq(True)
    m.next = IDLE

  with m.Elif(
    ((link.a.tag == Tag.EXT_VAL).bool() | (link.a.tag == Tag.ERASE).bool())
    & ((link.b.tag == Tag.EXT_VAL).bool() | (link.b.tag == Tag.ERASE).bool())
  ):
    # (ExtVal|Erase) >-< (ExtVal|Erase) - either erase an ext val,
    # or erase an eraser; nothing to do
    m.d.sync += link.done.eq(True)
    m.next = IDLE

  with m.Elif(
    (link.a.tag == Tag.COMB).bool() & (link.b.tag == Tag.COMB).bool()
    & (link.a.label == link.b.label).bool()
  ):
    # A and B have the same label, and will annihilate;
    # TODO add to fast worklist
    m.d.sync += Assert(False, "todo")

  with m.Elif(
    (link.a.tag == Tag.EXT_FN).bool() & (link.b.tag == Tag.EXT_FN).bool()
    & (link.a.label == link.b.label).bool()
  ):
    # TODO add to fast worklist
    m.d.sync += Assert(False, "todo")

  # TODO other cases
  with m.Else():
    m.d.sync += Assert(False, "all tag combinations should be handled")

def follow_wait(link: Link, m: Module):
  HeapPort.read(m, link.heap, link.b.payload)
  with m.If(link.heap.done):
    m.next = FOLLOW_READ

def follow_read(link: Link, m: Module):
  # `heap[b.payload]` is now available as `target`
  target = Port(link.heap.read_data)
  with m.If(target.tag == Tag.INVALID):
    # B points to an empty slot, just connect A >-< B
    m.next = CONNECT
  with m.Else():
    # B points to a slot with some port, so we:
    # - take out of that slot, and wait until alloc frees it
    AllocPort.free(m, link.alloc, link.b.payload)
    m.next = FOLLOW_WAIT_FREE

def follow_wait_free(link: Link, m: Module):
  AllocPort.free(m, link.alloc, link.b.payload)
  with m.If(link.alloc.done):
    m.next = DO_LINK

def connect(link: Link, m: Module):
  # connect wire A to port B
  with m.If(link.a == link.b):
    # free heap[a.payload]
    AllocPort.free(m, link.alloc, link.a.payload)
    m.next = CONNECT_WAIT_FREE
  with m.Else():
    # swap(&mut heap[a.payload], b)
    HeapPort.read(m, link.heap, link.a.payload)
    HeapPort.write(m, link.heap, link.a.payload, link.b)
    m.next = CONNECT_WAIT_WRITE

def connect_wait_free(link: Link, m: Module):
  AllocPort.free(m, link.alloc, link.a.payload)
  with m.If(link.alloc.done):
    m.d.sync += link.done.eq(True)
    m.next = IDLE


def connect_wait_write(link: Link, m: Module):
  HeapPort.read(m, link.heap, link.a.payload)
  HeapPort.write(m, link.heap, link.a.payload, link.b)
  with m.If(link.heap.done):
    # `heap[a.payload]` is available as `previous`
    previous = Port(link.heap.read_data)
    with m.If(previous.tag == Tag.INVALID):
      # no previous port at that slot
      m.d.sync += link.done.eq(True)
      m.next = IDLE
    with m.Else():
      # free the previous port at that slot
      AllocPort.free(m, link.alloc, link.a.payload)
      m.next = CONNECT_FINISH_LINK

def connect_finish_link(link: Link, m: Module):
  AllocPort.free(m, link.alloc, link.a.payload)
  with m.If(link.heap.done):
    m.d.sync += link.done.eq(True)
    m.next = IDLE
