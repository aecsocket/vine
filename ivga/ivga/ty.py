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
    COMB = 5

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
