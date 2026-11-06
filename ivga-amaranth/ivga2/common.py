import enum

from amaranth import *
from amaranth.lib import data

SYNC = "sync"
"""Clock domain `sync`."""
PERIOD = 1e-8
"""Simulator clock period: 10ns = 100MHz."""

WORD_WIDTH = 64
"""How many bits a machine word takes.

A word is the smallest generic unit that the machine operates on."""
Word = unsigned(WORD_WIDTH)
"""Machine word type."""

TAG_WIDTH = 3
"""How many bits a `Tag` takes."""

class Tag(enum.Enum, shape=TAG_WIDTH):
  """Identifies a kind of `Port`."""

  NONE = 0b000
  """No valid port at this address.

  This tag is used for runtime metadata for values which aren't actually real
  ports; specifically:
  - if this word is fresh memory that has never been written to, `payload` and
    `label` are all zeroes.
  - if this word has just been freed by the allocator, `payload` and `label`
    are all ones.
  - if this word is being used by the allocator to store the address of the next
    free word in the free list, `payload` stores the address to the next word
    and `label` is all zeroes."""

  WIRE = 0b001
  """Wire which joins to another port at address `payload`.

  `label` is always zeroed."""

  GRAFT = 0b010

  ERASE = 0b011
  """Eraser port.

  `payload` and `label` are always zeroed."""

  EXT_VAL = 0b100
  COMB = 0b101
  EXT_FN = 0b110

LABEL_WIDTH = 16
"""How many bits a `Port`'s label takes."""

class _AddrShape(Shape):
  @staticmethod
  def other_half(addr):
    """Given an address pointing to an auxiliary `Port` (either left or right half),
    returns the address pointing to the other half `Port`."""
    return addr ^ 1

  @staticmethod
  def left_half(addr):
    """Given an address pointing to an auxiliary `Port` (either left or right half),
    returns the address pointing to the left half `Port`."""
    return addr & ~1

ADDR_WIDTH = WORD_WIDTH - TAG_WIDTH - LABEL_WIDTH
ADDR_MAX = (1 << ADDR_WIDTH) - 1
Addr = _AddrShape(ADDR_WIDTH)

class Port(data.Struct):
  tag: Tag
  payload: unsigned(ADDR_WIDTH)
  label: unsigned(LABEL_WIDTH)

  @staticmethod
  def new(tag: Value, label: Value, payload: Value) -> "Port":
    def field(value, width):
      return Cat(value, Const(0, width))[:width]

    return Port(Cat(
      field(tag, TAG_WIDTH),
      field(payload, ADDR_WIDTH),
      field(label, 16)
    ))

  @staticmethod
  def aux(port) -> tuple["Port", "Port"]:
    """Return auxiliary wire views of a binary node's principal port."""
    return (
      Port.new(Const(Tag.WIRE), Const(0, 16), port.payload),
      Port.new(Const(Tag.WIRE), Const(0, 16), Addr.other_half(port.payload)),
    )

  @staticmethod
  def none():
    return Const({
      "tag": Tag.NONE,
      "payload": 0,
      "label": 0,
    }, Port)

  @staticmethod
  def with_addr(addr):
    payload = Cat(Value.cast(addr), Const(0, 45))[:45]
    return Port(Cat(Const(Tag.NONE, 3), payload, Const(0, 16)))

  @staticmethod
  def freed():
    return Const({
      "tag": Tag.NONE,
      "payload": (1 << 45) - 1,
      "label": (1 << 16) - 1,
    }, Port)

  @staticmethod
  def wire(addr):
    return Const({
      "tag": Tag.WIRE,
      "payload": addr,
      "label": 0,
    }, Port)

  @staticmethod
  def eraser():
    return Const({
      "tag": Tag.ERASE,
      "payload": 0,
      "label": 0,
    }, Port)

  @staticmethod
  def comb(addr, label):
    return Const({
      "tag": Tag.COMB,
      "payload": addr,
      "label": label,
    }, Port)

assert Port.as_shape().size == WORD_WIDTH
