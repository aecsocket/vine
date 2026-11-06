from amaranth.lib.memory import ReadPort, WritePort

from amflow import Capture, Flow, ValueLike


def memory_read(f: Flow, read: ReadPort, *, addr: ValueLike) -> Capture:
  result = f.capture_after_step(read.data)
  f.d.comb += [
    read.en.eq(True),
    read.addr.eq(addr),
  ]
  return result

def memory_write(f: Flow, write: WritePort, *, addr: ValueLike, data: ValueLike) -> None:
  f.d.comb += [
    write.en.eq(True),
    write.addr.eq(addr),
    write.data.eq(data),
  ]
