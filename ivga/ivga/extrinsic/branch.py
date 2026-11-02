from amaranth import *

from ivga import ADDR_MAX, Addr, Port
from ivga.extrinsic.common import ExtFn, ExtFnContext, ExtFnGen, ExtTy
from ivga.extrinsic.n32 import N32
from ivga.mem.heap import HeapPort
from ivga.runtime.linker import LinkerPort

BRANCH = "ivm:branch"

def branch(cx: ExtFnContext) -> ExtFnGen:
  f = cx.f
  a, b, out = f.capture(cx.a), f.capture(cx.b), f.capture(cx.out)
  index, base = a.payload[:32], b.payload

  def do_error():
    yield
    f.d.on_done += cx.error.eq(True)
    yield LinkerPort.link(f, cx.linker, a=out, b=Port.eraser())

  # TODO: I believe we can weaken some of these validity checks,
  # and enforce them at load time.

  valid = (a.payload <= 0xFFFF_FFFF) & (base <= ADDR_MAX)
  with f.If(valid):
    yield
    # branch table construction should validate that:
    # - `heap[base] = table_len` is the actual amount of entries in the table
    # - each entry in `heap[base + 1 : base + 1 + table_len]` is a valid
    #   `Tag.GRAFT` port which we can branch off
    table_len = yield HeapPort.read(f, cx.heap, addr=base)
    yield

    with f.If(index < table_len):
      # Register the bounds decision before entering heap arbitration again.
      yield
      # Keep address arithmetic separate from the shared heap payload mux.
      arm_addr = f.capture((base + 1 + index)[:Addr.width])
      yield
      arm = yield HeapPort.read(f, cx.heap, addr=arm_addr)
      yield
      arm = Port(arm)

      f.d.on_done += cx.error.eq(False)
      yield LinkerPort.link(f, cx.linker, a=out, b=arm)
    with f.Else():
      yield from do_error()
  with f.Else():
    yield from do_error()

EXT_TYS: list[ExtTy] = [
  ExtTy(BRANCH, is_copy=True)
]

EXT_FNS: list[ExtFn] = [
  ExtFn(
    "ivm:branch",
    inputs=(N32, BRANCH),
    fn=branch,
  )
]
