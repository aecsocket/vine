from amaranth import *
from amaranth.lib.memory import ReadPort, WritePort

from amflow import Flow, Step, ValueLike, step


def memory_read(f: Flow, read: ReadPort, *, addr: ValueLike) -> Step:
  result = f.capture(read.data, after_step=True)

  return step(
    f,
    comb=[
      read.en.eq(True),
      read.addr.eq(addr),
    ],
    out=result.value,
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
