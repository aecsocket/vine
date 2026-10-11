# Booba debugging handoff — private findings and spoiler policy

This document intentionally contains the detailed findings the user asked to preserve for future sessions. **Do not summarize its diagnosis or workaround in conversational replies unless the user explicitly asks for spoilers.** The user is debugging this themselves and may deliberately defer reading this file.

## Working agreement for future assistants

- Give incremental, discriminating hints when requested, not the answer or an unsolicited fix. Do not repeat experiments the user already performed.
- Do not edit application source, arithmetic, the viewer, or `README.md` unless explicitly asked. Creating this document is the only authorized repository change for this handoff.
- Preserve preexisting uncommitted edits. In particular, the current user's experimental contact stubs, commented updates, and scene changes are intentional; do not restore the investigation snapshot over them.
- Keep experiment edits in newly created directories under `target/`. Do not modify or clean up unrelated/preexisting build artifacts.
- Capture runtime output rather than printing detailed diagnostics or source changes into the conversation. Use neutral case labels/statuses when the user still wants no spoilers.
- Bound stdin and subprocess duration. Sending newlines requests simulation steps; merely starting the executable and leaving stdin open is not a bounded test.
- Copy **only the necessary Vine module directory**, not the entire `booba/` tree. That larger directory includes `.venv` and other unrelated artifacts. Earlier repeated broad copies produced a huge scratch tree, subsequently cleaned up.

## Status at this handoff

There is a **confirmed, reduced source-level trigger** and a behavior-preserving workaround tested against the investigation's original application snapshot. This is materially stronger evidence than a change correlated with success.

There is **not** a proven identification of the offending compiler transformation, interaction-net node, runtime algorithm, or library implementation defect. Also, the user's later source configurations differ from the tested snapshot and still require investigation. In particular, explicit writes to both bodies are a sufficient ingredient in the reduced reproducer, **not a proven necessary condition for every Booba failure**.

No application fix has been applied. The earlier investigation modified only scratch copies under `target/investigation_private`. Its original-source integrity checks passed before cleanup. The source now differs because the user has continued their own experiments; do not interpret those differences as assistant modifications or regressions to undo.

## Layout and correct invocation

Run from `/var/home/boris/Projects/vine`:

```sh
target/debug/vine run booba/booba/ --debug --no-stats --no-perf
```

The module entrypoint is the **directory** `booba/booba/`, whose entry file is `booba/booba/booba.vi`. The source-file entrypoint form:

```sh
target/debug/vine run booba/booba/booba.vi --debug --no-stats --no-perf
```

is not equivalent: it produced compile errors saying a source file cannot include submodules from another file. Do not treat that setup error as the runtime failure.

Relevant files:

- `booba/booba/booba.vi`: scene, frame output, input-driven step loop.
- `booba/booba/engine.vi`: integration and all-pairs contact traversal.
- `booba/booba/contact.vi`: contact dispatch, geometric helpers, resolution.
- `booba/booba/arithmetic/{arithmetic,rotation,unit,scalar,vector}.vi`: custom arithmetic.
- `booba/vicious_circle_test.vi`: simple nested-reference control using `Foo`.
- `booba/viewer.py`: subprocess driver and frame parser.
- `root/data/List.vi`: underlying reference iterator implementation, if investigating below the application level.

### Bounded command examples

One newline requests one update after the initial frame; stdin EOF lets the loop terminate. A simple one-step diagnostic invocation is:

```sh
printf '\n' | timeout 30s target/debug/vine run booba/booba/ --debug --no-stats --no-perf
```

Do not run that unredirected command in a spoiler-free session: it can expose diagnostics. Prefer captured subprocess execution, for example:

```python
import subprocess

result = subprocess.run(
    [
        "target/debug/vine", "run", "booba/booba/",
        "--debug", "--no-stats", "--no-perf",
    ],
    input="\n" * 256,
    capture_output=True,
    text=True,
    timeout=60,
)
# Inspect/store stdout and stderr privately. Print only a neutral case status.
```

A timeout is not a passing result. The current source has not been rerun as part of writing this document.

## Frame protocol and the visible symptoms

The currently inspected main loop:

1. Prints `FRAME`.
2. Prints one shape record for each body:
   - `CIRCLE x y radius`
   - `RECTANGLE x y half_width half_height`
3. Prints `END` and flushes.
4. Reads a line from stdin.
5. On `Some(_)`, calls `engine.update(1.0 / 60.0)` and continues; on EOF, exits.

Consequently, a successful run with 256 input newlines normally emits 257 complete frames: the initial frame plus one after each step. The original-snapshot workaround runs actually recorded 257 frames.

The viewer launches the module directory with `--no-stats --no-perf`, reads stdout on a separate thread, and requests each next step by writing and flushing `"\n"`. Its normal launch does not include `--debug`; the debug flag was used for the investigation. Stderr is not redirected into the viewer's frame parser. Keep stdout suitable for the protocol; additional stdout messages can themselves cause parser errors.

`read_frames` accepts the two shape tags above, validates finite fields and nonnegative sizes, and raises `Incomplete frame: missing END` if EOF arrives with a frame open. That error describes the truncated stream; it does not independently identify a physics or parser defect.

The retained original-failure capture, `target/investigation_private/AD06.out`, contains:

```text
FRAME
RECTANGLE 0.0 -10.0 100.0 5.0
CIRCLE 0.0 -3.95000005 1.0
CIRCLE 0.0 -2.17464805 1.0
END
FRAME

Error: the net did not return its `IO` handle

Error: the net created a vicious circle
```

Thus the first frame was complete, the next frame started but did not complete, and the runtime reported both the missing returned `IO` handle and the vicious circle. This supports treating an accompanying viewer incomplete-frame error as a downstream symptom of failed net evaluation, rather than fixing the parser to hide it. The capture combines stdout and stderr; its displayed ordering is not a proof of a total execution order across those streams. The exact dependency relating the IO failure to the cycle has not been traced at net-node level.

## Confirmed investigation snapshot versus current user source

### Snapshot used for the completed controlled investigation

The tested scene had a static rectangular floor and two dynamic circles. The floor's inverse mass was `0.0`; each circle's was `0.5`. Integration and the contact-resolution writes were enabled. `resolve_contacts` contained debug field logs before and after contact computation. `find_contacts` matched the pair of collider values received through reference parameters.

The original traversal was:

```vine
for (i, body_a) in engine.bodies.iter_ref().enumerate() {
  for body_b in engine.bodies.iter_ref().skip(i + 1) {
    contact::resolve_contacts(body_a, body_b);
  }
}
```

The original module failed with a single input newline. A floor plus just one circle succeeded. The user's later confirmation of that two-versus-three-body difference was not new evidence requiring another identical hint.

**Static floor caveat:** `inverse_mass_sum` is the sum of both bodies' inverse masses. A static floor paired with a dynamic circle does not make this sum zero; the sum-zero guard applies when both bodies are static (assuming nonnegative inverse masses). Do not attribute a floor/circle return to that guard without inspecting the actual values.

### Current source inspected while writing this document

The source has changed since the investigation:

- `booba.vi` currently has the floor commented out and three explicitly initialized moving circles, with zero gravity. The old generated rows are commented out.
- `engine.vi` still uses the original nested reference traversal. Its `Engine.update` currently calls both `integrate_bodies` and `resolve_all_contacts`; integration's writes are currently enabled.
- `contact.vi` currently has the velocity and position updates for **both** bodies commented out. The earlier numbered debug logs have been removed.
- The active `find_contacts` currently ignores its inputs and returns a constant `Some(ContactManifold(...))`, with `UnitVec2::X`, a zero-position/zero-depth first point, and no second point.
- The original collider-pair dispatch has been retained under the name `find_contacts_`; it is not the function called by `resolve_contacts` at this inspection point.
- `resolve_contacts` still obtains fields through both body references, consumes the contact option with a `None` early return, checks the inverse-mass sum, and retains the later conditional calculations.

These are observed source contents, not a newly executed test result. Do not claim the current constant-`Some` variant passes or fails based solely on this document.

### Latest user-reported observations

After receiving limited hints, the user reported that commenting out both A/B updates **and** disabling integration still left a vicious circle. Preserve this as a user-observed configuration, distinct from the current file inspection (where integration is enabled again) and from the earlier assistant-tested snapshot.

The user suggested that Ivy has no imperative mutation concept in the usual sense and that a reference still carries the ability to write even when used for reads. The useful conceptual distinction is:

- Removing explicit assignment syntax does not necessarily remove reference value/space or writeback threading.
- A reference participates in value and space/writeback dataflow; this is not a claim that each read performs a mutation.
- Read-only-looking source can still have cyclic dependencies.
- Source order and function boundaries do not by themselves impose completed reduction boundaries.

The latest no-explicit-writes observation means the reduced dual-write example must not be advertised as an exhaustive necessary-and-sufficient characterization of the current source. It remains a confirmed, very small sufficient trigger, and the traversal alteration remains a tested remedy for the earlier full snapshot. Whether it fixes the latest user variant has not been rerun here.

## Concrete established trigger and limits of the diagnosis

In the controlled snapshot, a pair operation that conditionally writes through both element references, executed while an **outer live `List::IterRef`** traverses the same list, forms a runtime causality cycle with three elements. That exact combination is demonstrated without any physics code.

The decisive discriminator is that replacing the **outer** reference iterator with an index loop obtaining `List::at(i)` succeeds while retaining the original inner `iter_ref().skip(i + 1)` and the same pair function. Replacing only the inner iterator does not succeed. This is not merely evidence that changing arbitrary syntax helps.

Do not generalize this to “two `iter_ref` loops are invalid.” The simple `Foo` test has two such loops and passes. Do not identify `skip`, the collider tuple match, the geometry helper, early `return` syntax, debug logging, or rotation as the independently proven underlying cause. The evidence specifically isolates the outer live reference traversal's interaction with the pair operation's branching/reference dependencies. The actual compiler/runtime/library mechanism remains to be traced.

### Why the low-level origin remains uncertain

The inspected `List::IterRef` representation is `(N32, &Stream[T])`. Its advance operation uses raw/unsafe access to `cursor.*.head()` and `cursor.*.tail()`, returning an element reference alongside a live tail iterator. Fork and Drop are declared for this iterator.

The retained outer iterator/tail is a plausible place to investigate causal ordering and conditional reference writeback. This is an inference from controlled cases and implementation structure, not a completed proof of the precise unsafe-library or compiler defect. No generated-net tracing established the responsible node. Whether the idiom violates an intended reference-causality contract, exposes a library implementation limitation, or reveals a compiler/runtime bug is unresolved.

## Minimal reproducer and controls

The retained `target/investigation_private/Z03.vi`, rerun as AD00, is:

```vine
fn act(&a: &F32, &b: &F32) {
  if a < b { return; }
  a += 1.0;
  b -= 1.0;
}
pub fn main(&io: &IO) {
  let items = [1.0, 2.0, 3.0];
  for (i, a) in items.iter_ref().enumerate() {
    for b in items.iter_ref().skip(i + 1) { act(a, b); }
  }
  for x in items { io.println("{x}"); }
}
```

Run it as a standalone source file, since it has no file-based submodules:

```sh
target/debug/vine run target/investigation_private/Z03.vi --debug --no-stats --no-perf
```

It exits with the vicious-circle diagnostic. Every ascending-input pair should take the return branch, so no numeric updates should execute in an ordinary sequential interpretation. This itself demonstrates why “the writes cannot execute, therefore they cannot affect reference dependencies” is unsafe reasoning. Descending input also fails.

### Controlled reduced-case matrix

| Case | Change from the relevant reduced baseline | Observed result |
| --- | --- | --- |
| Z03 / AD00 | Three F32 elements; conditional pair operation; both writes; original nested traversal | Cycle, exit 1 |
| Z04 / AD02 | Only two elements | Pass, exit 0 |
| Z05 / AD03 | Remove the conditional return; retain both writes | Pass, exit 0 |
| Z06 | Retain condition, remove only B's write | Pass |
| Z07 | Retain condition, remove only A's write | Pass |
| Z08 | Descending values `[3.0, 2.0, 1.0]` | Cycle |
| Z09 | Use a conditional update block instead of early-return control flow | Cycle |
| AA00 | Independent scalar variables; call the same pair operation in the same pair order | Pass |
| AA02 / AC02 | Obtain both pair refs with indexed `List::at` | Pass |
| AA03 | Pop the outer value, pair against remaining values, append it | Pass |
| AB01 | Retain outer IterRef; only inner access becomes indexed | Cycle |
| AB02 | Inner enumerate/filter instead of skip; retain nested IterRef | Cycle |
| AC01 / AD01 | Only outer traversal becomes indexed; retain inner iterator and skip | Pass, exit 0 |
| W02 / Z00 | Earlier enum/struct reduction; remove logging | Cycle |
| X02 / X05 | Earlier enum/struct reduction; owned enum arguments or local snapshots | Cycle |

The attempted AA01 Array variant did not compile. It says nothing about whether Array iteration would avoid the failure.

### Passing reduced counterpart

`target/investigation_private/AC01.vi` retains the same `act` function but uses:

```vine
pub fn main(&io: &IO) {
  let items = [1.0, 2.0, 3.0];
  for i in 0..3 {
    let a = match items.at(i) {
      Some(a) { a }
      None() { continue; }
    };
    for b in items.iter_ref().skip(i + 1) { act(a, b); }
  }
  for x in items { io.println("{x}"); }
}
```

Both failing and passing reduced programs were rerun after the initial experiments, with bounded captured execution.

### Passing simple nested-reference control

`booba/vicious_circle_test.vi` uses three `Foo(N32)` values, the original nested enumerate/skip reference loops, and a `bar` function that prints both numbers. It passed as G0 and AD07. It does not exercise the same conditional writeback dependencies as the failing reduced pair operation. It proves that a sweeping prohibition on nested reference iteration is unsupported.

## Full-application workaround — documented, NOT applied

The preferred tested scratch alteration changes the outer loop only:

```vine
fn resolve_all_contacts(&engine: &Engine) {
  let count: N32 = List::len(&engine.bodies);
  for i in 0..count {
    let body_a = match engine.bodies.at(i) {
      Some(body) { body }
      None() { continue; }
    };
    for body_b in engine.bodies.iter_ref().skip(i + 1) {
      contact::resolve_contacts(body_a, body_b);
    }
  }
}
```

This is a suggested workaround, not an authorization to edit the user source. The retained scratch engine containing it is `target/investigation_private/E01.vi` (original experiment AE00). Keep the user's current code untouched unless asked.

Important details:

- AE00 ran the original floor-plus-two-circles snapshot for 256 updates, emitted 257 frames, and exited 0.
- It kept all original contact dispatch, contact calculations, debug logs, integration, and body updates.
- Explicit `List::len` with a `N32` result avoids the observed ambiguity of `.len()` in the presence of the application's imported vector `Len` trait.
- The pair set and order remain the same: increasing outer index, then all later bodies.
- List indexed access is linear; this is a correctness workaround, not a broadphase/performance redesign.

Other full-snapshot workarounds passed for 256 input newlines, with 257 recorded frames:

1. AC00 / AD05: numeric loops with indexed `.at(i)` and `.at(j)` for both body references.
2. Y00 / AD04: remove the outer body with `pop_front`, pair it against `take(count - i - 1)` of the remaining list, then append it with `push_back`. After the sweep the original order is restored, and pairs are visited in the same order. This is more restructuring than the outer-index alteration.

A larger triangular four-row scene, eleven total bodies, was tested as AE01. It timed out at 60 seconds with 67 `FRAME` markers recorded, out of the 257 expected for 256 steps. This is **not** a pass, and the marker count does not prove each corresponding frame was complete. It also does not identify why the larger run failed to finish: expensive evaluation, another dependency issue, or something else was not distinguished. Do not claim broad-workload validation from this case.

## Negative controls on the original application snapshot

These experiments are historical; most individual scratch trees were removed during cleanup. Their outcomes are preserved in the private report and selected result files.

- I0 / Y01 / AD06: unchanged original application reproduces the cycle. AD06 uses only one input newline.
- L00: removing all debug logs does not fix it.
- L01–L05: removing individual logs does not fix it.
- L10: changing `find_contacts` collider arguments and parameters to values does not fix it.
- L12: replacing the relative rotation with identity does not fix it.
- T00: removing integration entirely does not fix it.
- T01–T03: removing individual integration writes does not fix it.
- N00: borrowing local collider copies does not fix it.
- N01: caching inverse masses before the call does not fix it.
- N02: rewriting the collider tuple match as nested matches does not fix it.
- R00–R03: disabling individual dispatcher arms, including making all original match arms return `None`, does not fix it while retaining that dispatch/reference structure and the later mutation continuation.
- M00: replacing the **entire body** of `find_contacts` with an unconditional `None()` succeeds. This avoids dependencies and removes collision behavior; it is a discriminator, not a valid simulation fix.
- M01–M06: shortened `resolve_contacts` prefixes that discard the contact result or never use it for the later conditional mutations succeed. Because evaluation is demand-driven, these results do not prove the original contact computation was fully forced.
- V00: removing frame body rendering does not fix it.
- V01: floor plus one circle passes.
- V02: explicit references in the frame-rendering loop do not fix it.

The distinction between a constant function body and several dispatch arms returning the same constant matters: retaining reference destructuring, branching, and reassembly can retain dependencies even when the apparent result is identical. Do not infer that the exact collision function is an isolated net whose internal demand/reference behavior cannot affect surrounding logic.

## Arithmetic inspection and rejected explanations

The investigation inspected the then-current `rotation.vi`, `unit.vi`, `vector.vi`, and `scalar.vi`. Rotation composition/inverse and normalization remained unchanged in the successful full-snapshot alterations. Disabling integration or substituting identity relative rotation did not eliminate the original failure. The F32-only reduced reproducer needs none of those modules.

Therefore these arithmetic modules are not necessary triggers for the established failure; no arithmetic fixes were proposed. This is not a claim to have exhaustively verified every mathematical property or every subsequent user edit.

Earlier hypotheses about borrowed collider tuple matching, local copies, rotation, or log placement were superseded by the reduced scalar evidence. Owned collider arguments alone were not a remedy. `skip` is not essential either: the enumerate/filter control still failed.

## Demand, references, branches, and logging

The user initially observed that logging both bodies before `find_contacts` could work, whereas logging after it exposed a cycle. They also observed that removing a return could remove earlier visible logs.

Do not reason from those observations as if source lines are strictly reduced to completion in imperative order. Logging and continuation changes alter which field projections, discriminants, reference spaces/writebacks, and IO chains are demanded. An unused contact result may never be forced. A branch can carry conditional reconstruction of referenced values even if an ordinary sequential reading says its explicit updates will not execute.

The reduced cycle has no debug logging, so logs are not a necessary cause. Moving logs can expose or hide a dependency; it is not a validated simulation correction. Likewise, an early return is not an independently proven faulty construct: replacing it with a conditional update block still fails in the reduced case.

The user’s latest read-only-looking failure deserves its own controlled reproduction. Removing assignment lines does not automatically erase all reference/branch dependencies, but that conceptual observation is not a net-level diagnosis of their exact current variant. Do not dismiss it as impossible, and do not claim it was independently reproduced by the assistant.

## Chronology and spoiler-free conversation state

1. The user requested a private, scratch-only investigation and explicitly prohibited revealing the exact cause.
2. Initial file-mode attempts failed at setup. An early overly broad cycle classifier matched `circle` in filenames/diagnostics. Those D/F setup cases are **not runtime evidence**; later decisive cases matched the actual `vicious\\s+circle` diagnostic and used the module directory.
3. The application module was reproduced; source-level hypotheses were tested and reduced to an F32-only conditional pair update.
4. Discriminating controls established the outer-iterator interaction and three-element trigger for that reduction. The full original scene passed with the outer-index scratch alteration.
5. The unchanged original application and the standalone `Foo` control were rerun, and original-source equality was checked.
6. The user requested cleanup. Scratch experiment copies were deleted, preserving only a small report/evidence set.
7. The first conversational hint was floor-plus-one-circle versus three bodies; the user had already tried it. Do not repeat it as new guidance.
8. The next hint was to preserve the decision but remove updates to one body at a time. This was grounded in the reduced controls, not a promise that removing all writes solves every application variant.
9. The user reported a cycle after removing both A/B updates and integration, and discussed reference value/space semantics. The response distinguished removal of explicit writes from removal of reference threading, without giving the traversal workaround.
10. The latest narrow hint was to keep `find_contacts`' signature/caller and replace its **whole body** with `None()`. The current inspection shows experimentation with a constant `Some(...)` body instead; do not assume the result of a `None()` test unless the user supplies it or a new authorized test is run.
11. The user explicitly authorized writing this detailed handoff, but still wants spoiler-free conversational replies and no application edits.

## Retained evidence and cleanup

The evidence directory is `target/investigation_private/`. The cleaned directory had 16 files totaling 19,785 bytes at the recorded cleanup checkpoint; 280,810 of 280,826 entries were removed. The large repeated copies had occupied about 7.15 GB of logical file contents. No unrelated/preexisting target files were cleaned up.

Retained artifacts:

- `report.md`: original private findings, including historical case references and cleanup notes.
- `Z03.vi`: failing scalar reduction.
- `AC01.vi`: passing outer-index scalar counterpart.
- `E01.vi`: preferred modified engine from the successful original-scene scratch run.
- `Z_results.json`, `AC_results.json`: reduced/control statuses.
- `AD_results.json`: final reruns, original module failure, standalone control success, and two full-scene passing alternatives.
- `AE_results.json`: preferred workaround success and larger-scene timeout.
- `Y_results.json`: additional full-scene status evidence.
- `AD00.out`, `AD01.out`, `AD06.out`, `AD07.out`: selected captured outputs.
- `source_integrity.json`, `source_hashes.json`: historical integrity evidence. The hash manifest was narrowed to nine Vine source files during cleanup.
- `cleanup_summary.json`: scope/count/size record.

Paths to AE00, AC00, Y00 and other experiment **directories** in the historical report/results are no longer live. The full application copies, experiment runners, intermediate captures, and implementation-context collection were removed. Outputs larger than 16 KiB were eligible for reduction to first/last 4 KiB during cleanup; result JSON retains execution statuses. Do not promise a complete historical raw trace where it was not retained.

The original integrity checks applied to the original investigation snapshot. They are not a requirement that the user's subsequently edited source match the hashes today. Avoid reconstructing that snapshot by overwriting current user work.

## Sensible next investigation steps, if authorized

These are private planning notes, not permission to reveal the answer or edit source:

- Reproduce the user's exact current no-explicit-write/constant-contact variant in a bounded, source-only scratch copy, and record the configuration before comparing it with historical results.
- Distinguish an unconditional `None` body, an unconditional `Some` body, and the original dispatcher without changing multiple unrelated factors at once.
- If the user wants the mechanism rather than a workaround, trace the generated net around the outer iterator's retained stream reference and the pair call's conditional reference reconstruction. Read the language's reference-causality contract before labeling the behavior a compiler bug.
- Validate the documented outer-index alteration against the user's **current** variant only when asked; its historical 256-step success is evidence for the earlier snapshot, not automatic proof for all later edits.
- A future scratch copy should contain only the nine relevant Vine files/module tree, not `.venv`, images, caches, or other unrelated artifacts.

For now, this is a documentation-only handoff. No runtime validation was run while writing it, and no application source, README, existing scratch evidence, or preexisting target file was changed.
