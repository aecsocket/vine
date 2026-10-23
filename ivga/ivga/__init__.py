import amaranth as _am
from amaranth.lib import data as _data
from amaranth.lib import enum as _enum

# clock domain "sync"
SYNC = "sync"

# machine word size
WORD_WIDTH = 64
Word = _am.unsigned(WORD_WIDTH)

ADDR_WIDTH = 10
ADDR_MAX = (1 << ADDR_WIDTH) - 1
Addr = _am.unsigned(ADDR_WIDTH)

# hardware statistics counter (e.g. num of allocs/frees)
STAT_WIDTH = 32
Stat = _am.unsigned(STAT_WIDTH)

# ports
class Tag(_enum.Enum, shape=3):
  INVALID = 0
  WIRE = 1
  GRAFT = 2
  ERASE = 3
  EXT_VAL = 4
  COMB = 5
  EXT_FN = 6


class Port(_data.Struct):
  tag: Tag
  payload: _am.unsigned(45)
  label: _am.unsigned(16)

  # unsafe if used on a port that's not
  # a principal port of Comb or ExtVal
  @staticmethod
  def aux(m: _am.Module, port):
    one, two = _am.Signal(Port), _am.Signal(Port)
    m.d.comb += [
      one.tag.eq(Tag.WIRE),
      one.payload.eq(port.payload),
      one.label.eq(0),

      two.tag.eq(Tag.WIRE),
      two.payload.eq(port.payload + 1),
      two.label.eq(0),
    ]
    return one, two


assert Port.as_shape().size == WORD_WIDTH


class Port2(_data.Struct):
  a: Port
  b: Port


assert Port2.as_shape().size == WORD_WIDTH * 2

ERASER = {"tag": Tag.ERASE, "payload": 0, "label": 0}

PORT_EMPTY = _am.Const(
  {
    "tag": Tag.INVALID,
    "payload": 0,
    "label": 0,
  },
  Port,
)

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
