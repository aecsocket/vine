from collections.abc import Callable, Generator, Iterable
from enum import Enum
from typing import Any, Self

from amaranth import *
from amaranth import ValueCastable
from amaranth.hdl._ast import Statement, Switch

from amflow.capture import Capture, CaptureReads, CaptureWrites

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
    self.loop_depth = 0

  def capture(self, value: ValueLike, *, after_step: bool = False) -> Capture:
    """Capture on entry here, or in the continuation of the pending step."""
    captured = Capture(value)
    self.captures.append(captured)
    if after_step:
      self.after_step.append(captured)
    else:
      _flush_effects(self)
      self.block.items.append(captured)
    self.m.d.comb += captured.bypass.eq(False)
    return captured

  def capture_after_step(self, value: ValueLike) -> Capture:
    return self.capture(value, after_step=True)

  def If(self, cond: ValueLike) -> FlowIf:
    return FlowIf(self, cond)

  def Elif(self, cond: ValueLike) -> FlowElif:
    return FlowElif(self, cond)

  def Else(self) -> FlowElse:
    return FlowElse(self)

  def While(self, cond: ValueLike) -> FlowWhile:
    return FlowWhile(self, cond)

  def Break(self) -> None:
    """Exit the innermost While without introducing a clock boundary."""
    if self.loop_depth == 0:
      raise ValueError("`Break` requires an enclosing `While`")
    _flush_effects(self)
    self.block.items.append(Break())


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
    self.items: list[Assigns | Branch | Loop | Break | Capture] = []

class Assigns:
  """List of assignments to add to `m.d` during elaboration of a state."""

  def __init__(
    self,
    comb: list[Statement],
    sync: list[Statement],
    on_done: list[Statement],
    until: Value,
    after_step: list[Capture] | None = None,
  ):
    self.comb = comb
    self.sync = sync
    self.on_done = on_done
    self.until = until
    self.after_step = [] if after_step is None else after_step

class Effects(Assigns):
  """Assignments at a control-flow position, without a clock boundary."""


def _take_effects(flow: Flow, kind: type[Assigns]) -> Assigns:
  after_step, flow.after_step = flow.after_step, []
  return kind(
    flow.d.comb.take(), flow.d.sync.take(), flow.d.on_done.take(),
    flow.d.until.take(), after_step,
  )


def _always(condition: Value) -> bool:
  return isinstance(condition, Const) and bool(condition.value)


def _flush_effects(flow: Flow) -> None:
  effects = _take_effects(flow, Effects)
  if effects.comb or effects.sync or effects.on_done or effects.after_step or not _always(effects.until):
    flow.block.items.append(effects)


class Branch:
  def __init__(self):
    self.arms: list[tuple[Value, Block]] = []
    self.or_else: Block | None = None

class Loop:
  def __init__(self, cond: Value):
    self.cond = cond
    self.body = Block()

class Break:
  """A zero-time exit to the innermost loop's continuation."""


# control flow

class FlowIf:
  def __init__(self, flow: Flow, cond: ValueLike):
    self.flow = flow
    self.cond = Value.cast(cond)

  def __enter__(self):
    _flush_effects(self.flow)
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
    try:
      if exc_type is None:
        _flush_effects(self.flow)
    finally:
      self.flow.block = self.parent
    return False

class FlowElif:
  def __init__(self, flow: Flow, cond: ValueLike):
    self.flow = flow
    self.cond = Value.cast(cond)

  def __enter__(self):
    _flush_effects(self.flow)
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
    try:
      if exc_type is None:
        _flush_effects(self.flow)
    finally:
      self.flow.block = self.parent
    return False

class FlowElse:
  def __init__(self, flow: Flow):
    self.flow = flow

  def __enter__(self):
    _flush_effects(self.flow)
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
    try:
      if exc_type is None:
        _flush_effects(self.flow)
    finally:
      self.flow.block = self.parent
    return False

class FlowWhile:
  def __init__(self, flow: Flow, cond: ValueLike):
    self.flow = flow
    self.cond = Value.cast(cond)

  def __enter__(self):
    _flush_effects(self.flow)
    self.parent = self.flow.block
    self.loop = Loop(self.cond)
    self.parent.items.append(self.loop)

    # future accumulations go in this body
    self.flow.block = self.loop.body
    self.flow.loop_depth += 1
    return self

  def __exit__(self, exc_type, exc, traceback):
    try:
      if exc_type is None:
        _flush_effects(self.flow)
    finally:
      self.flow.block = self.parent
      self.flow.loop_depth -= 1
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
    flow.block.items.append(_take_effects(flow, Assigns))

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

  def emit_statements(domain: str, statements: Iterable[Statement]) -> None:
    for stmt in writes.on_statements(statements):
      if isinstance(stmt, Switch):
        with m.Switch(stmt.test):
          for patterns, body, _ in stmt.cases:
            with m.Default() if patterns is None else m.Case(*patterns):
              emit_statements(domain, body)
      else:
        m.d[domain] += stmt

  def bind(assigns: Assigns, forwarded: tuple[Capture, ...]) -> Assigns:
    reads = CaptureReads(flow.captures, forwarded)
    return Assigns(
      list(reads.on_statements(writes.on_statements(assigns.comb))),
      list(reads.on_statements(writes.on_statements(assigns.sync))),
      list(reads.on_statements(writes.on_statements(assigns.on_done))),
      reads.read(assigns.until), assigns.after_step,
    )

  def merge(groups: Iterable[Assigns]) -> Assigns:
    result = Assigns([], [], [], Const(True))
    for group in groups:
      result.comb.extend(group.comb)
      result.sync.extend(group.sync)
      result.on_done.extend(group.on_done)
      result.after_step.extend(group.after_step)
      if _always(result.until):
        result.until = group.until
      elif not _always(group.until):
        result.until = result.until.bool() & group.until.bool()
    return result

  def block_effects(block: Block) -> Assigns | None:
    groups = []
    for item in block.items:
      if isinstance(item, Effects) and not item.after_step:
        groups.append(item)
      elif isinstance(item, Branch):
        group = branch_effects(item)
        if group is None:
          return None
        groups.append(group)
      else:
        return None
    return merge(groups)

  def branch_effects(branch: Branch) -> Effects | None:
    result = block_effects(branch.or_else) if branch.or_else is not None else merge(())
    if result is None:
      return None
    for cond, body in reversed(branch.arms):
      arm = block_effects(body)
      if arm is None:
        return None

      def select(
        yes: list[Statement], no: list[Statement], condition: Value = cond,
      ) -> list[Statement]:
        if not yes and not no:
          return []
        return [Switch(condition.bool(), [((1,), yes, None), (None, no, None)])]

      until = Const(True) if _always(arm.until) and _always(result.until) else Mux(
        cond, arm.until.bool(), result.until.bool(),
      )
      result = Effects(
        select(arm.comb, result.comb), select(arm.sync, result.sync),
        select(arm.on_done, result.on_done), until,
      )
    return Effects(result.comb, result.sync, result.on_done, result.until)

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

  class Effecting:
    def __init__(self, effects: Assigns, following: Target):
      self.effects = effects
      self.following = following

  # None is the end of the program, which loops back to its entry.
  type Target = Action | Decision | Capturing | Effecting | None

  def lower(block: Block, after: Target, break_to: Target = None) -> Target:
    entry = after
    for item in reversed(block.items):
      if isinstance(item, Effects):
        entry = Effecting(item, entry)
      elif isinstance(item, Assigns):
        entry = Action(item, entry)
      elif isinstance(item, Capture):
        entry = Capturing(item, entry)
      elif isinstance(item, Loop):
        # The body's continuation is its own condition, not the following item.
        loop = Decision([], entry)
        loop.arms.append((item.cond, lower(item.body, loop, break_to=entry)))
        entry = loop
      elif isinstance(item, Break):
        entry = break_to
      else:
        effects = branch_effects(item)
        if effects is not None:
          # A branch with no clock boundaries is an ordinary within-step guard.
          if effects.comb or effects.sync or effects.on_done or not _always(effects.until):
            entry = Effecting(effects, entry)
        else:
          otherwise = lower(item.or_else, entry, break_to) if item.or_else is not None else entry
          arms = [(cond, lower(body, entry, break_to)) for cond, body in item.arms]
          if all(target is otherwise for _, target in arms):
            entry = otherwise
          else:
            entry = Decision(arms, otherwise)
    return entry

  entry = lower(flow.root, None)
  if entry is None:
    return

  states: list[Action | Decision | Capturing | Effecting] = []
  names: dict[Action | Decision | Capturing | Effecting, str] = {}

  def schedule(target: Target) -> str:
    if target is None:
      target = entry
    assert target is not None
    if target not in names:
      names[target] = str(len(states))
      states.append(target)
    return names[target]

  # Pending effects retain both their originals for retries and position-bound
  # reads for this edge; a later capture must not change an earlier effect.
  def emit(
    target: Target,
    path: tuple[tuple[Decision | Capturing | Effecting, frozenset[int]], ...] = (),
    pending: tuple[tuple[Assigns, Assigns], ...] = (),
    replay: tuple[Assigns, ...] = (),
    forwarded: tuple[Capture, ...] = (),
  ) -> None:
    if target is None:
      target = entry
    assert target is not None

    if isinstance(target, (Decision, Capturing, Effecting)):
      position = (target, frozenset(id(captured) for captured in forwarded))
      if position in path:
        if any(group.after_step for _, group in pending):
          raise ValueError("Uncommitted `after_step` captures; did you forget to `yield`?")
        if any(group.on_done or not _always(group.until) for _, group in pending):
          raise ValueError("A control-flow cycle with completion effects must reach a `yield`")
        m.next = schedule(target)
        return
      path = (*path, position)

    if isinstance(target, Effecting):
      effects = target.effects
      bound = bind(effects, forwarded)
      emit_statements("comb", bound.comb)
      emit_statements("sync", bound.sync)
      emit(target.following, path, (*pending, (effects, bound)), (*replay, effects), forwarded)
      return

    if isinstance(target, Capturing):
      captured = target.captured
      reads = CaptureReads(flow.captures, forwarded)
      m.d.comb += captured.bypass.eq(True)
      emit_statements("sync", [captured.reg.eq(reads.read(Value.cast(captured.source)))])
      emit(target.following, path, pending, replay, (*forwarded, captured))
      return

    if isinstance(target, Decision):
      # Entry-state effects stay at the dispatch; pending handshakes keep running.
      replay = tuple(group for group in replay if not _always(group.until))
      reads = CaptureReads(flow.captures, forwarded)
      for i, (cond, arm) in enumerate(target.arms):
        condition = reads.read(cond)
        with m.If(condition) if i == 0 else m.Elif(condition):
          emit(arm, path, pending, replay, forwarded)
      with m.Else():
        emit(target.otherwise, path, pending, replay, forwarded)
      return

    assigns = target.assigns
    bound = bind(assigns, forwarded)
    emit_statements("comb", bound.comb)
    emit_statements("sync", bound.sync)
    completion = merge((*[group for _, group in pending], bound))
    following = target.following
    for captured in reversed(completion.after_step):
      following = Capturing(captured, following)

    if _always(completion.until):
      emit_statements("sync", completion.on_done)
      m.next = schedule(following)
    else:
      retry = target
      if pending:
        repeated = merge((*replay, assigns))
        original = merge((*[group for group, _ in pending], assigns))
        retry = Action(Assigns(
          repeated.comb, repeated.sync, original.on_done, original.until,
        ), following)
      with m.If(completion.until):
        emit_statements("sync", completion.on_done)
        m.next = schedule(following)
      with m.Else():
        m.next = schedule(retry)

  first_state = schedule(entry)
  with m.FSM(init=first_state):
    i = 0
    while i < len(states):
      with m.State(str(i)):
        emit(states[i])
      i += 1
