from ivga.extrinsic import branch, n32
from ivga.extrinsic.common import ExtFn, ExtTy

EXT_TYS: list[ExtTy] = n32.EXT_TYS + branch.EXT_TYS
EXT_FNS: list[ExtFn] = n32.EXT_FNS + branch.EXT_FNS
