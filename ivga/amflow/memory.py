from amaranth.lib.memory import ReadPort, WritePort

from amflow import Flow, ValueLike


def memory_read(f: Flow, read: ReadPort, *, addr: ValueLike) -> ValueLike:
  result = f.capture(read.data, after_step=True)

  f.accumulate(comb=[
    read.en.eq(True),
    read.addr.eq(addr),
  ])
  return result

def memory_write(f: Flow, write: WritePort, *, addr: ValueLike, data: ValueLike) -> None:
  f.accumulate(comb=[
    write.en.eq(True),
    write.addr.eq(addr),
    write.data.eq(data),
  ])
