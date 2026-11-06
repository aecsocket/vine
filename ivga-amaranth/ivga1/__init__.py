import amaranth as _am
from amaranth.lib import data as _data
from amaranth.lib import enum as _enum
from amaranth.lib import wiring

SYNC = "sync"
"""Clock domain `sync`."""
PERIOD = 1e-8
"""Simulator clock period: 10ns = 100MHz."""

WORD_WIDTH = 64
"""How many bits a machine word takes.

A word is the smallest generic unit that the machine operates on."""
Word = _am.unsigned(WORD_WIDTH)
"""Machine word type."""

class _AddrShape(_am.Shape):
  @staticmethod
  def other_half(addr):
    return addr ^ 1

  @staticmethod
  def left_half(addr):
    return addr & ~1

ADDR_WIDTH = 10
ADDR_MAX = (1 << ADDR_WIDTH) - 1
Addr = _AddrShape(ADDR_WIDTH)


# hardware statistics counter (e.g. num of allocs/frees)
STAT_WIDTH = 32
"""How many bits a hardware statistic counter takes."""
Stat = _am.unsigned(STAT_WIDTH)
"""Shape of a hardware statistic counter.

This shape is used for tracking e.g. number of page allocations, frees,
interactor commutations, etc."""

class Tag(_enum.Enum, shape=3):
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

# first bit of the payload; right after the tag
HALF_BIT = 0b1000

class Port(_data.Struct):
  tag: Tag
  payload: _am.unsigned(45)
  label: _am.unsigned(16)

  @staticmethod
  def new(tag: _am.Value, label: _am.Value, payload: _am.Value) -> "Port":
    def field(value, width):
      return _am.Cat(_am.Value.cast(value), _am.Const(0, width))[:width]

    return Port(_am.Cat(
      field(tag, 3),
      field(payload, 45),
      field(label, 16)
    ))

  @staticmethod
  def aux(port) -> tuple["Port", "Port"]:
    """Return auxiliary wire views of a binary node's principal port."""
    return (
      Port.new(_am.Const(Tag.WIRE), _am.Const(0, 16), port.payload),
      Port.new(_am.Const(Tag.WIRE), _am.Const(0, 16), Addr.other_half(port.payload)),
    )

  @staticmethod
  def none():
    return _am.Const({
      "tag": Tag.NONE,
      "payload": 0,
      "label": 0,
    }, Port)

  @staticmethod
  def with_addr(addr):
    payload = _am.Cat(_am.Value.cast(addr), _am.Const(0, 45))[:45]
    return Port(_am.Cat(_am.Const(Tag.NONE, 3), payload, _am.Const(0, 16)))

  @staticmethod
  def freed():
    return _am.Const({
      "tag": Tag.NONE,
      "payload": (1 << 45) - 1,
      "label": (1 << 16) - 1,
    }, Port)

  @staticmethod
  def wire(addr):
    return _am.Const({
      "tag": Tag.WIRE,
      "payload": addr,
      "label": 0,
    }, Port)

  @staticmethod
  def eraser():
    return _am.Const({
      "tag": Tag.ERASE,
      "payload": 0,
      "label": 0,
    }, Port)

  @staticmethod
  def comb(addr, label):
    return _am.Const({
      "tag": Tag.COMB,
      "payload": addr,
      "label": label,
    }, Port)


assert Port.as_shape().size == WORD_WIDTH


class Port2(_data.Struct):
  a: Port
  b: Port


assert Port2.as_shape().size == WORD_WIDTH * 2


def flatten(it, nested: wiring.Signature):
  """Takes all members of the `nested` signature, and assigns them to their
  same name in `it`.

  Let's say you have:
  ```python
  class MyModulePort(wiring.Signature):
    def __init__(self):
      super().__init__({
        "some_signal": In(1),
        "some_signal_2": In(1),
      })

  class MyModule(wiring.Component);
    control: In(MyModulePort())
  ```

  If you want to access `my_module.some_signal`, normally you have to go
  through `my_module.control.some_signal`. If you run:

  ```python
  class MyModule(wiring.Component);
    control: In(MyModulePort())

    def __init__(self):
      flatten(self, self.control)
  ```

  You can now access `my_module.some_signal` directly.
  """

  for name in nested.signature.members:
    setattr(it, name, getattr(nested, name))
