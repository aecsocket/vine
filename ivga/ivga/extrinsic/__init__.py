from amaranth import *

from ivga.extrinsic import branch, n32
from ivga.extrinsic.common import ExtFn, ExtTy

EXT_TYS: list[ExtTy] = n32.EXT_TYS + branch.EXT_TYS
EXT_FNS: list[ExtFn] = n32.EXT_FNS + branch.EXT_FNS

COPY_MASK = sum(
  int(ty.is_copy) << i
  for i, ty in enumerate(EXT_TYS)
)
"""Bitfield of which `ExtTy`s in `EXT_TYS` are copyable.

If bit `i` is set, `EXT_TYS[i].is_copy`."""

def label_is_copy(label) -> Value:
  """Get a `Value` representing if this `label`, taken from a `Port.label`
  with `Port.tag == Tag.EXT_VAL`, is for a copyable `ExtTy`."""

  value = Const(COPY_MASK, len(EXT_TYS)).bit_select(label, 1)
  return Value.cast(value)
