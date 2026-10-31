from collections.abc import Callable, Generator, Iterable
from enum import Enum
from typing import Any

from amaranth import *
from amaranth import ValueCastable
from amaranth.hdl._ast import Statement

__all__ = [
  "Flow",
  "ValueLike",
  "expand",
]

type ValueLike = Value | ValueCastable | Enum | int

class Flow:
  def __init__(self, m: Module):
    self.m = m
    self.comb: list[Statement] = []
    self.sync: list[Statement] = []

  def accumulate(
    self,
    *,
    comb: Statement | Iterable[Statement] = (),
    sync: Statement | Iterable[Statement] = (),
  ):
    self.comb.extend([comb] if isinstance(comb, Statement) else comb)
    self.sync.extend([sync] if isinstance(sync, Statement) else sync)

  def If(self, cond: Value):
    pass

  def Else(self):
    pass

  def Elif(self, cond: Value):
    pass

class Step:
  def __init__(self, *, until: Value, out: Value | None):
    self.until = until
    self.out = out

def step(
  f: Flow,
  *,
  comb: Statement | Iterable[Statement] = (),
  sync: Statement | Iterable[Statement] = (),
  until: Value | None = None,
  out: Value | None = None,
) -> Step:
  f.accumulate(comb=comb, sync=sync)
  return Step(
    until=Const(True) if until is None else until,
    out=out,
  )

def sync(f: Flow, assigns: Statement | Iterable[Statement]) -> Step:
  return step(f, sync=assigns)

def comb(f: Flow, assigns: Statement | Iterable[Statement]) -> Step:
  return step(f, comb=assigns)

def tick(f: Flow) -> Step:
  return step(f)

def expand(
  m: Module,
  make_gen: Callable[
    [Flow],
    Generator[
      Step,
      Any, # pyright: ignore[reportExplicitAny]
      None,
    ]
  ],
):
  class State:
    def __init__(self, flow: Flow, step: Step):
      self.comb = flow.comb.copy()
      self.sync = flow.sync.copy()
      self.until = step.until

  flow = Flow(m)
  gen = make_gen(flow)
  result = None
  states = []

  while True:
    try:
      step = gen.send(result)
      states.append(State(flow, step))
      result = step.out

      flow.comb.clear()
      flow.sync.clear()
    except StopIteration:
      if flow.comb:
        raise ValueError("Uncommitted `comb` effects; did you forget to `yield`?")
      if flow.sync:
        raise ValueError("Uncommitted `sync` effects; did you forget to `yield`?")

      break

  if not states:
    # nothing to elaborate
    return

  with m.FSM(init=str(0)):
    for i, state in enumerate(states):
      with m.State(str(i)):
        m.d.comb += state.comb
        with m.If(state.until):
          m.d.sync += state.sync
          m.next = str((i + 1) % len(states))
