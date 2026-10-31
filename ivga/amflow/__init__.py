from collections.abc import Callable, Generator, Iterable
from enum import Enum
from typing import Any, Self

from amaranth import *
from amaranth import ValueCastable
from amaranth.hdl._ast import Statement

from amflow.capture import Capture, CaptureWrites

__all__ = [
  "Capture",
  "Flow",
  "ValueLike",
  "expand",
]

type ValueLike = Value | ValueCastable | Enum | int

class Flow:
  def __init__(self, m: Module):
    self.m = m
    # control flow tree
    self.root = Block()
    self.block = self.root # branch we're currently accumulating into
    # accumulator for actions to take this step
    self.d = FlowDomains()
    self.after_step: list[Capture] = []
    self.captures: list[Capture] = []

  def capture(self, value: ValueLike, *, after_step: bool = False) -> Capture:
    """Capture on entry here, or in the continuation of the pending step."""
    captured = Capture(value)
    self.captures.append(captured)
    if after_step:
      self.after_step.append(captured)
    else:
      self.block.items.append(captured)
    self.m.d.comb += captured.bypass.eq(False)
    return captured


  def If(self, cond: ValueLike) -> FlowIf:
    return FlowIf(self, cond)

  def Elif(self, cond: ValueLike) -> FlowElif:
    return FlowElif(self, cond)

  def Else(self) -> FlowElse:
    return FlowElse(self)

  def While(self, cond: ValueLike) -> FlowWhile:
    return FlowWhile(self, cond)


class FlowDomains:
  def __init__(self):
    self.comb = FlowDomain()
    self.sync = FlowDomain()
    self.on_done = FlowDomain()
    self.until = UntilDomain()


class FlowDomain:
  def __init__(self):
    self.statements: list[Statement] = []

  def __iadd__(self, stmts: Statement | Iterable[Statement]) -> Self:
    self.statements.extend([stmts] if isinstance(stmts, Statement) else stmts)
    return self

  def take(self) -> list[Statement]:
    statements, self.statements = self.statements, []
    return statements

class UntilDomain:
  def __init__(self):
    self.condition: Value = Const(True)

  def __iadd__(self, value: ValueLike) -> Self:
    condition: Value = Value.cast(value)
    if isinstance(self.condition, Const) and self.condition.value:
      self.condition = condition
    else:
      self.condition = self.condition.bool() & condition.bool()
    return self

  def take(self) -> Value:
    condition, self.condition = self.condition, Const(True)
    return condition

class Block:
  """List of operations to perform during elaboration, which may branch out
  into more conditional blocks or control flow structures.
  """

  def __init__(self):
    self.items: list[Assigns | Branch | Loop | Capture] = []

class Assigns:
  """List of assignments to add to `m.d` during elaboration of a state."""

  def __init__(
    self,
    comb: list[Statement],
    sync: list[Statement],
    on_done: list[Statement],
    until: Value,
  ):
    self.comb = comb
    self.sync = sync
    self.on_done = on_done
    self.until = until

class Branch:
  def __init__(self):
    self.arms: list[tuple[Value, Block]] = []
    self.or_else: Block | None = None

class Loop:
  def __init__(self, cond: Value):
    self.cond = cond
    self.body = Block()

# control flow

class FlowIf:
  def __init__(self, flow: Flow, cond: ValueLike):
    self.flow = flow
    self.cond = Value.cast(cond)

  def __enter__(self):
    self.parent = self.flow.block

    # make a new branch
    self.branch = Branch()
    self.body = Block()
    self.branch.arms.append((self.cond, self.body))
    self.parent.items.append(self.branch)

    # future accumulations go in this body
    self.flow.block = self.body
    return self

  def __exit__(self, exc_type, exc, traceback):
    self.flow.block = self.parent
    return False

class FlowElif:
  def __init__(self, flow: Flow, cond: ValueLike):
    self.flow = flow
    self.cond = Value.cast(cond)

  def __enter__(self):
    self.parent = self.flow.block

    # reuse the last existing branch
    if not self.parent.items:
      raise ValueError("`Elif` requires a preceding `If`/`Elif`")
    branch = self.parent.items[-1]
    if not isinstance(branch, Branch):
      raise TypeError("Preceding item must be a branch")
    if branch.or_else is not None:
      raise ValueError("Cannot add `Elif` after `Else`")
    self.body = Block()
    branch.arms.append((self.cond, self.body))

    # future accumulations go in this body
    self.flow.block = self.body
    return self

  def __exit__(self, exc_type, exc, traceback):
    self.flow.block = self.parent
    return False

class FlowElse:
  def __init__(self, flow: Flow):
    self.flow = flow

  def __enter__(self):
    self.parent = self.flow.block

    # reuse the last existing branch
    if not self.parent.items:
      raise ValueError("`Else` requires a preceding `If`/`Elif`")
    branch = self.parent.items[-1]
    if not isinstance(branch, Branch):
      raise TypeError("Preceding item must be a branch")
    if branch.or_else is not None:
      raise ValueError("Cannot add `Else` after `Else`")
    self.body = Block()
    branch.or_else = self.body

    # future accumulations go in this body
    self.flow.block = self.body
    return self

  def __exit__(self, exc_type, exc, traceback):
    self.flow.block = self.parent
    return False

class FlowWhile:
  def __init__(self, flow: Flow, cond: ValueLike):
    self.flow = flow
    self.cond = Value.cast(cond)

  def __enter__(self):
    self.parent = self.flow.block
    self.loop = Loop(self.cond)
    self.parent.items.append(self.loop)

    # future accumulations go in this body
    self.flow.block = self.loop.body
    return self

  def __exit__(self, exc_type, exc, traceback):
    self.flow.block = self.parent
    return False


# expansion

def expand(
  m: Module,
  make_gen: Callable[
    [Flow],
    Generator[
      object,
      Any, # pyright: ignore[reportExplicitAny]
      None,
    ]
  ],
):
  flow = Flow(m)
  gen = make_gen(flow)
  result = None

  def _finish_step() -> None:
    flow.block.items.append(Assigns(
      flow.d.comb.take(),
      flow.d.sync.take(),
      flow.d.on_done.take(),
      flow.d.until.take(),
    ))
    after_step, flow.after_step = flow.after_step, []
    flow.block.items.extend(after_step)

  while True:
    try:
      result = gen.send(result)
      _finish_step()
    except StopIteration:
      if flow.d.comb.statements:
        raise ValueError("Uncommitted `comb` effects; did you forget to `yield`?")
      if flow.d.sync.statements:
        raise ValueError("Uncommitted `sync` effects; did you forget to `yield`?")
      if flow.d.on_done.statements:
        raise ValueError("Uncommitted `on_done` effects; did you forget to `yield`?")
      if flow.after_step:
        raise ValueError("Uncommitted `after_step` captures; did you forget to `yield`?")
      until: Value = flow.d.until.condition
      if not isinstance(until, Const) or not until.value:
        raise ValueError("Uncommitted `until` condition; did you forget to `yield`?")
      break

  writes = CaptureWrites(flow.captures)

  class Action:
    def __init__(self, assigns: Assigns, following: Target):
      self.assigns = assigns
      self.following = following

  class Decision:
    def __init__(
      self,
      arms: list[tuple[Value, Target]],
      otherwise: Target,
    ):
      self.arms = arms
      self.otherwise = otherwise

  class Capturing:
    def __init__(self, captured: Capture, following: Target):
      self.captured = captured
      self.following = following

  # None is the end of the program, which loops back to its entry.
  type Target = Action | Decision | Capturing | None

  def lower(block: Block, after: Target) -> Target:
    entry = after
    for item in reversed(block.items):
      if isinstance(item, Assigns):
        entry = Action(item, entry)
      elif isinstance(item, Capture):
        entry = Capturing(item, entry)
      elif isinstance(item, Loop):
        # The body's continuation is its own condition, not the following item.
        loop = Decision([], entry)
        loop.arms.append((item.cond, lower(item.body, loop)))
        entry = loop
      else:
        otherwise = lower(item.or_else, entry) if item.or_else is not None else entry
        arms = [(cond, lower(body, entry)) for cond, body in item.arms]
        # A branch whose arms all fall through has no work to dispatch.
        if all(target is otherwise for _, target in arms):
          entry = otherwise
        else:
          entry = Decision(arms, otherwise)
    return entry

  entry = lower(flow.root, None)
  if entry is None:
    return

  states: list[Action | Decision | Capturing] = []
  names: dict[Action | Decision | Capturing, str] = {}

  def schedule(target: Target) -> str:
    if target is None:
      target = entry
    assert target is not None
    if target not in names:
      names[target] = str(len(states))
      states.append(target)
    return names[target]

  def emit(target: Target, path: tuple[Decision | Capturing, ...] = ()) -> None:
    if target is None:
      target = entry
    assert target is not None

    if isinstance(target, (Decision, Capturing)):
      if target in path:
        # Close a path with no yielded action at the repeated control-flow node.
        m.next = schedule(target)
        return
      path = (*path, target)

    if isinstance(target, Capturing):
      captured = target.captured
      m.d.comb += captured.bypass.eq(True)
      m.d.sync += captured.reg.eq(captured.source)
      # Capture at the first edge, even if the following action stalls.
      emit(target.following, path)
      return

    if isinstance(target, Decision):
      for i, (cond, arm) in enumerate(target.arms):
        with m.If(cond) if i == 0 else m.Elif(cond):
          emit(arm, path)
      with m.Else():
        emit(target.otherwise, path)
      return

    assigns = target.assigns
    m.d.comb += writes.on_statements(assigns.comb)
    m.d.sync += writes.on_statements(assigns.sync)

    # optimization for `Const(True)` and similar
    if isinstance(assigns.until, Const) and assigns.until.value:
      m.d.sync += writes.on_statements(assigns.on_done)
      m.next = schedule(target.following)
    else:
      with m.If(assigns.until):
        m.d.sync += writes.on_statements(assigns.on_done)
        m.next = schedule(target.following)
      with m.Else():
        # Commit to this action instead of reconsidering its branch next cycle.
        m.next = schedule(target)

  first_state = schedule(entry)
  with m.FSM(init=first_state):
    i = 0
    while i < len(states):
      with m.State(str(i)):
        emit(states[i])
      i += 1
