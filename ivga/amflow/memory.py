from amaranth import *
from amaranth.lib.memory import ReadPort, WritePort

from amflow import Flow, Step, ValueLike, step


def memory_read(f: Flow, read: ReadPort, *, addr: ValueLike) -> Step:
  m = f.m
  issued = Signal()
  just_read = Signal()
  saved = Signal.like(read.data)

  # store the value we just read in a register,
  # in case we're chaining memory reads and need to keep our read alive
  # after the next read
  m.d.sync += just_read.eq(issued)
  with m.If(just_read):
    m.d.sync += saved.eq(read.data)

  # the latest value is:
  # - the read port's `data`, if we've just completed a read cycle
  # - the value saved on the last read cycle, if not
  latest = Mux(just_read, read.data, saved)

  return step(
    f,
    comb=[
      issued.eq(True),
      read.en.eq(True),
      read.addr.eq(addr),
    ],
    out=latest,
  )

def memory_write(f: Flow, write: WritePort, *, addr: ValueLike, data: ValueLike) -> Step:
  return step(
    f,
    comb=[
      write.en.eq(True),
      write.addr.eq(addr),
      write.data.eq(data),
    ]
  )
