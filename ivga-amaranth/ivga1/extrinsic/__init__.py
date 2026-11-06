from amaranth import *

from ivga1.extrinsic import branch, n32
from ivga1.extrinsic.common import ExtFn, ExtTy

EXT_TYS: list[ExtTy] = n32.EXT_TYS + branch.EXT_TYS
EXT_FNS: list[ExtFn] = n32.EXT_FNS + branch.EXT_FNS
TY_IDS = {ty.name: i for i, ty in enumerate(EXT_TYS)}

SWAP_BIT = 1 << 15
"""Bit of a `ExtFn` index representing whether this instantiation of an
extrinsic function has its argument order swapped."""

assert len(EXT_FNS) <= SWAP_BIT

FN_ID_MASK = SWAP_BIT - 1

COPY_MASK = sum(
  int(ty.is_copy) << i
  for i, ty in enumerate(EXT_TYS)
)
"""Bitfield of which `ExtTy`s in `EXT_TYS` are copyable.

If bit `i` is set, `EXT_TYS[i].is_copy`."""

def ext_val_label_is_copy(label) -> Value:
  """Get a `Value` representing if this `label`, taken from a `Port.label`
  with `Port.tag == Tag.EXT_VAL`, is for a copyable `ExtTy`."""

  value = Const(COPY_MASK, len(EXT_TYS)).bit_select(label, 1)
  return Value.cast(value)
