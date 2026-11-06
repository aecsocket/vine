# Working on amflow

`amflow` is a deliberately small generator-based control-flow layer over Amaranth. Keep clock
boundaries explicit, avoid control-only bubbles, and do not grow it into a general-purpose HLS
compiler or transaction scheduler.

The user maintains `README.md` by hand: do not edit it without explicit permission. Do not add test
files unless requested; inline simulations and existing validation workflows can verify changes.
Preserve unrelated work in this repository.

## Execution model

```python
expand(m, lambda f: program(f, component))
```

`expand()` constructs a `Flow` and consumes the entire generator during elaboration. Python does
not resume the generator at runtime. The resulting hardware executes the recorded program.

- Calls add effects or other nodes to the recorded program.
- A `yield` closes the current group and introduces a completion boundary.
- The yielded Python object is sent back unchanged. Helpers return hardware values directly;
  there is no `Step` token or `.out` property.
- A yielded group takes at least one active clock edge and can stall for additional cycles.
- A yield is not necessarily a change in the physical FSM state value. Loop iterations may reuse
  states, and stalled operations can need internal wait-state transitions.
- The end of the program loops back to its entry; no mandatory finished state is inserted.
- Generator helpers compose with `yield from`. Ordinary helpers simply record logic and return.

Use `f.If`, `f.Elif`, `f.Else`, and `f.While` for runtime hardware control flow. Ordinary Python
conditionals and loops execute during elaboration. Branch-local Python assignments do not create
hardware result merging or phi nodes; use explicit hardware storage when needed.

## Domains and clock boundaries

| Domain | Meaning |
| --- | --- |
| `f.d.comb += statements` | Drive combinational logic while its emitted state/path is active. |
| `f.d.sync += statements` | Update registers at active edges; can repeat while an action waits. |
| `f.d.on_done += statements` | Update registers only at the group's completing edge. |
| `f.d.until += predicate` | Add a completion condition. All accumulated predicates must hold. |

The statement domains accept either one statement or an iterable. `until` takes a value-like
predicate, not an assignment list. Its default is true.

```python
f.d.comb += request.eq(True)
f.d.on_done += completed.eq(completed + 1)
f.d.until += ready
yield
```

While waiting, the request remains active and `completed` does not increment repeatedly.
For an always-completing action, `sync` and `on_done` execute at the same edge.

Assignments remain hardware assignments, not sequential Python mutations:

```python
f.d.sync += counter.eq(counter + 1)
f.d.sync += counter.eq(counter + 1)
yield
```

This increments once. Both right-hand sides read the pre-edge value, and assignment priority
resolves overlapping writes. Use separate yields for separate increments.

A synchronous assignment also retains its value after leaving the state. For a state-qualified
ready/grant signal, use `comb`; assigning true through `sync` does not automatically clear it.

## Effects flushing preserves source position

Each domain owns its pending storage. `take()` transfers that storage and resets the accumulator.
There is no public `Flow.accumulate()` or `Flow.finish_step()` method.

Pending domains are flushed:

- Before entering a control-flow context, into the current parent block.
- On normal exit from a context, into its child block before restoring the parent.
- Before recording an immediate capture.
- At a yield, into a clocked `Assigns` node through the private finalizer in `expand()`.

Structural flushes produce zero-time `Effects` nodes. **Flushing records location; it is not a
clock boundary and does not by itself execute completion effects.**

```python
f.d.comb += granted.eq(True)
with f.If(request_present):
  yield do_work(f)
```

The grant effect belongs before the branch. It is not stolen by the first yield Python happens
to encounter inside one arm. This allows an entry/idle state to expose readiness while the chosen
operation's later states remain busy.

Entering `Elif` or `Else` must attach to the immediately preceding branch. Do not put intervening
effects or other operations between arms.

## Two kinds of conditional

### Effects-only conditional

```python
with f.If(condition):
  f.d.sync += value.eq(1)
yield
```

A branch containing only effects and nested effects-only branches is lowered to guarded
within-step assignments. It consumes no separate decision cycle. Its conditions remain live
while the enclosing action waits.

This supports multiple conditional lane updates followed by one shared yield. Neither arm needs
its own yield merely to retain its guard.

The implementation's effects-only classification excludes captures, deferred captures, loops,
and clocked `Assigns` nodes. Do not assume that every syntactically yield-free block is a plain
combinational guard.

### Temporal conditional

```python
with f.If(condition):
  yield operation_a(f)
with f.Else():
  yield operation_b(f)
```

The branch decision and the selected first action share a state. If that action stalls, the
compiler commits to the selected action and enters a dedicated retry state. The original
condition must not switch branches while waiting or be rechecked in later branch states.

Ordinary entry-prefix `comb` and `sync` effects are not replayed after this commitment. Pending
`on_done`, completion conditions, and deferred captures still belong to the first completion
boundary. The retry replays the operation's active effects.

Replay is batch-based: prefix batches with a nontrivial `until` are retained to keep pending
handshakes active. Keep idle-only signals in their own entry prefix, and place a blocking
resource's request effects and predicate together in its selected operation. Literal nonzero
`Const` conditions are recognized as always-completing; do not casually change this detection
without checking entry/retry behavior.

## Loops

`f.While(condition)` records its body once. Lowering makes the last body action continue back to
the loop condition; it does not rerun Python to construct each iteration.

- Test the condition before starting an iteration.
- Fuse that test with the first body action.
- On a false condition, fall through into the continuation without a condition-only cycle.
- `f.Break()` exits the nearest enclosing `f.While()` without a clock boundary. It is a plain
  recording call, not something to yield. Use `with f.If(...)` to make it conditional.
- `Break` flushes preceding effects at their source position. Lowering replaces the remainder of
  that path with the loop's continuation, carrying pending work to the next reached yield. Code
  after a taken break in that loop body is unreachable in hardware, even though Python still
  executes it while recording the program. A branch containing a break is not effects-only.
- Calling `Break` outside a loop is an elaboration error. Nested loops pass their own exit
  continuation during lowering, so an inner break cannot accidentally exit the outer loop.
- Once an iteration starts, finish its body even if its original condition changes while an
  action stalls.
- Plain no-action polling paths can idle at their control-flow node. A cycle carrying unresolved
  `on_done`, `until`, or deferred-capture work must reach a yield; it is rejected otherwise.

## Captures and synchronous memory

`f.capture(source)` returns a value-like proxy with backing storage:

```python
remaining = f.capture(request_count)
with f.While(remaining != 0):
  f.d.on_done += remaining.eq(remaining - 1)
  yield do_work(f)
```

On entry, reads forward `source`. At the first active edge, the source is stored, even if the
following action stalls. Later reads use the stored value. Completion assignments emitted later
can override initialization at that same edge. Loop back-edges skip captures outside the loop;
reaching a capture node again reinitializes it.

Capture reads inside Flow are bound at each control-flow position. Before a capture node is
visited in the current state, reads use its register; after visiting it, reads forward its source.
This lets a loop exit using an old captured count and immediately re-enter the program with a
fresh count, without making the exit condition depend on its own bypass selector. Pending effects
keep their position-bound reads; retry states rebind the original effects to stored captures.
Zero-time cycle detection includes the set of forwarded captures, so a revisit after fresh
captures is not mistaken for an unchanged control-flow cycle. No extra clock boundary is added.

Capture does not add a setup cycle. It also does not make arbitrary synchronous assignments
visible before their edge: `sync(counter = 4)` before a loop still cannot make the loop condition
see the new register value in that same cycle. Capture provides an explicit bypass for this.

`f.capture(source, after_step=True)` schedules the capture in the next completion's continuation.
All deferred captures in a grouped operation are retained, not just the last helper's result.

For a synchronous memory read:

1. Issue the request at edge N.
2. Immediately after edge N, the response is available and forwarded in the continuation.
3. At edge N+1, save it while a consumer or another read may commit concurrently.

Capturing `read.data` before the request edge would save the old response. Do not move deferred
captures across that boundary or introduce an unnecessary response-wait cycle for fixed-latency
ports. Raw synchronous read enables can default high; provide explicit module-level inactive
defaults when needed.

## Capture proxy implementation

`capture.py` handles operators, writable projections, and structured shapes. Arithmetic and
comparisons read the forwarded expression; `.eq()` writes backing storage. Slices, bit/word
selections, fields, and array elements preserve the read/write split. `.value` and `.reg` remain
available for compatibility and explicit access.

Native `Cat`, `Array`, and `Mux` destinations cast away proxy wrappers. `CaptureWrites` therefore
rewrites only assignment-LHS paths before `amflow` emits them. It must preserve RHS expressions,
dynamic offsets, switch selectors, shape/signedness, and source locations. The cached read marker
is recognized by identity, not HDL equality or structural mux matching.

Direct whole/projection `.eq()` calls can be used with a regular Amaranth module. Composite writes
outside `amflow` must explicitly use backing registers, such as `Cat(captured.reg, other)`.
Escaped composite writes fail safely instead of assigning through the bypass into the source.
This machinery uses Amaranth 0.5's internal AST; validate compatibility when changing versions.

## Compiler structure and limits

The pipeline is: generator collection -> structured blocks -> continuation-linked targets ->
flat, explicitly connected FSM states. Lower blocks backward so both branch arms know their
common continuation. Preserve dispatch/action fusion, empty-arm fallthrough, and retry-state
branch commitment. Never infer all transitions from adjacent state-list indices.

`f.m` is the real module. Direct `f.m.d.*` statements are elaborated immediately under any active
Amaranth contexts, not recorded as program-local effects. Use them deliberately for global
support logic and defaults.

This is not automatic arbitration or independent-handshake scheduling. Conjoining live grants
does not remember earlier acceptance. Concurrent requests that finish at different times need
per-request tracking, request deassertion, and retained results, or must be serialized. Fixed
one-cycle heap responses and variable-latency allocator completion are different helper contracts.

When validating changes, check edge timing rather than only final values: first-action issue,
back-to-back reads, guarded batching, stalled-branch commitment, entry-only grants, capture/retry
behavior, and shared continuations. Use distinguishable branch effects, simulate backpressure,
and check RTLIL conversion and diagnostics where appropriate.
