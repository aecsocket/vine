from amaranth import *
from amaranth.lib import data, enum

# clock domain "sync"
SYNC = "sync"

# machine word size
WORD_WIDTH = 64
Word = unsigned(WORD_WIDTH)

ADDR_WIDTH = 10
Addr = unsigned(ADDR_WIDTH)

# ports
class Tag(enum.Enum, shape=3):
    INVALID = 0
    WIRE = 1
    GRAFT = 2
    ERASE = 3
    EXT_VAL = 4
    COMB = 5
    EXT_FN = 6

class Port(data.Struct):
    tag: Tag
    payload: unsigned(45)
    label: unsigned(16)

assert Port.as_shape().size == WORD_WIDTH

ERASER = {
  "tag": Tag.ERASE,
  "payload": 0,
  "label": 0
}

PORT_EMPTY = Const({
    "tag": Tag.INVALID,
    "payload": 0,
    "label": 0,
}, Port)

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
