from collections.abc import Callable

from amaranth import *

from ivga import Port, Tag
from ivga.extrinsic.common import ExtFn, ExtFnContext, ExtFnMake, ExtTy
from ivga.runtime.linker import LinkerPort

N32 = "ivm:n32"

def arith(fn: Callable[[Value, Value], Value]) -> ExtFnMake:
  def run(cx: ExtFnContext):
    ty_n32 = cx.ty_id[N32]

    a = cx.a.payload[:32]
    b = cx.b.payload[:32]
    result = Signal(32)
    cx.f.d.comb += result.eq(fn(a, b))
    out = Port.new(Const(Tag.EXT_VAL), Const(ty_n32, 16), result)
    yield LinkerPort.link(cx.f, cx.linker, a=cx.out, b=out)
  return run

EXT_TYS: list[ExtTy] = [
  ExtTy(N32, is_copy=True),
]

EXT_FNS: list[ExtFn] = [
  ExtFn(
    "vi:n32:add",
    inputs=(N32, N32),
    fn=arith(lambda a, b: a + b),
  ),
  ExtFn(
    "vi:n32:sub",
    inputs=(N32, N32),
    fn=arith(lambda a, b: a - b),
  ),
  ExtFn(
    "vi:n32:eq",
    inputs=(N32, N32),
    fn=arith(lambda a, b: a == b),
  ),
  ExtFn(
    "vi:n32:ne",
    inputs=(N32, N32),
    fn=arith(lambda a, b: a != b),
  ),
]
