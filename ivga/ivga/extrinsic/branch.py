from ivga.extrinsic.common import ExtFn, ExtFnContext, ExtFnGen, ExtTy
from ivga.extrinsic.n32 import N32

BRANCH = "ivm:branch"

def branch(cx: ExtFnContext) -> ExtFnGen:
  pass

EXT_TYS: list[ExtTy] = [
  ExtTy("ivm:branch", is_copy=True)
]

EXT_FNS: list[ExtFn] = [
  ExtFn(
    "ivm:branch",
    inputs=(N32, BRANCH),
    fn=branch,
  )
]
