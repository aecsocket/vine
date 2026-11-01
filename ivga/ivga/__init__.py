import amaranth as _am
from amaranth.lib import data as _data
from amaranth.lib import enum as _enum

# clock domain "sync"
SYNC = "sync"

# machine word size
WORD_WIDTH = 64
Word = _am.unsigned(WORD_WIDTH)

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
Stat = _am.unsigned(STAT_WIDTH)

# ports
class Tag(_enum.Enum, shape=3):
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

# first bit of the payload;
# right after the tag
HALF_BIT = 0b1000

class Port(_data.Struct):
  tag: Tag
  payload: _am.unsigned(45)
  label: _am.unsigned(16)

  # unsafe if used on a port that's not
  # a principal port of Comb or ExtVal
  @staticmethod
  def aux(m, port) -> tuple[_am.Signal, _am.Signal]:
    one, two = _am.Signal(Port), _am.Signal(Port)
    m.d.comb += [
      one.tag.eq(Tag.WIRE),
      one.payload.eq(port.payload),
      one.label.eq(0),

      two.tag.eq(Tag.WIRE),
      two.payload.eq(Addr.other_half(port.payload)),
      two.label.eq(0),
    ]
    return one, two

  @staticmethod
  def none():
    return _am.Const({
      "tag": Tag.NONE,
      "payload": 0,
      "label": 0,
    }, Port)

  @staticmethod
  def with_addr(addr):
    # Extend/truncate the address to the payload width without a temporary signal.
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


# let's say you have:
#
#   class MyModulePort(wiring.Signature):
#     def __init__(self):
#       super().__init__({
#         "some_signal": In(1),
#         "some_signal_2": In(1),
#       })
#
#   class MyModule(wiring.Component);
#     control: In(MyModulePort())
#
# if you want to access `some_signal`, you have to go through
# `my_module.control.some_signal` every time - that's really annoying and verbose.
#
# if you run this after your module's super init, then you can just do:
#
#   my_module.some_signal
#   my_module.some_signal_2
def flatten(it, control):
  for name in control.signature.members:
    setattr(it, name, getattr(control, name))
