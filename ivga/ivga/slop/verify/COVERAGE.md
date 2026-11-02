# Formal optimization baseline

Run from `ivga/`:

```sh
python -m ivga.slop.verify
python -m ivga.slop.verify --list
python -m ivga.slop.verify 'memory_*' 'flow_*'
python -m ivga.slop.verify.selftest
```

Requires native `yosys`, `yosys-smtbmc`, and `z3` on PATH. SymbiYosys is not
required. The suite was developed against Amaranth 0.5.10 and Yosys 0.67.
The default run contains 88 cases: 35 inductive proofs and 53 bounded checks.
All passed on the development run, which took about 7.5 minutes. Eight additional
slow cases and two known failing specifications are opt-in, not included in that
passing count.

## What PASS means

- **PROVED**: initialized bounded base cases and temporal induction both passed.
  The safety properties hold indefinitely for the modeled configuration and
  stated environment contracts.
- **BOUNDED**: all symbolic behaviors within the displayed number of steps were
  checked from initialization. It is not an unbounded proof. Transaction harnesses
  assert completion deadlines as well as checking output values; a machine that
  never issues a request or never finishes cannot pass by implication alone.
- **Covers reached**: every declared cover was reachable within the same bound.
  Most harnesses require meaningful operations, branches, stalls, or completion.
  The two full-heap inductive safety cases have covers disabled; the separate
  timeline and routing cases exercise operation reachability.
- **FAIL**: assertion failure, unreachable cover, unsatisfiable assumptions,
  elaboration/tool failure, or timeout. These are not converted into skips or
  expected passes. Open the named phase log to distinguish them.

`--presat` checks assumption satisfiability. The separate runner self-test also
checks that false assertions, contradictory assumptions, and impossible covers
are rejected for the expected reason, not merely because a tool crashed.
Timeouts and interrupts terminate the solver process group.

Artifacts are stored in `build/slop_verify/<case>/`: RTLIL, SMT2, phase logs,
and VCD counterexamples/witnesses when produced by SMTBMC. Successful induction
runs discard intermediate failed-k traces; cover waveforms are reachability
witnesses, not bugs. `summary.json` records
modes, bounds, results, durations, and excluded slow cases for the last CLI run.
No waveform is evidence of a passing proof by itself; consult the status/log.

## Coverage

| Group | Checked behavior | Scope |
| --- | --- | --- |
| `arbiter_*` | Independent round-robin reference, mutual exclusion, request qualification, priority changes, stalls, acceptance spacing, fairness | Inductive; combinational/registered widths 1, 2, 3, 5, 15 |
| `resource_arbiter_*` | Command muxing, owner tracking, isolation, broadcasts/exclusions, latency-1/2 response routing | BMC; 2/3 clients and symbolic resource |
| `memory_heap_*` | Actual 1024x64 Heap contents, arbitrary addresses/data, read-first collisions, two-edge response, output hold, real HeapArbiter integration | Inductive watched-word invariants plus bounded timeline; separate abstract-token routing proof |
| `memory_stack_*` | Real Stack LIFO data/control, full/empty, overflow/underflow, push priority, arbiter response routing | Inductive; depths 1/2/3/5 with 64-bit words and depth 2 with packed 128-bit Port2 |
| `alloc_page_*` | Real PageAlloc reservations, bump uniqueness, OOM, free/reuse, operand capture and counters | BMC scripts; exhausted reserved-prefix invariant inductive |
| `alloc_pair_*` | Real PairAlloc/PageAlloc, bounds/alignment, bookkeeping, half frees, reuse, reclamation, rollback | Small allocation case in default BMC; expensive scenarios opt-in, several still unverified |
| `flow_*` | Real amflow sync/on_done, capture projections/composite writes, temporal versus effects guards, countdowns, nested Break, HeapPort read/swap/batching/reuse without replay | BMC with symbolic inputs, held-data poison and bounded readiness |
| `grafter_*` | Real Grafter rejects zero/oversized register counts, header error, NONE root, footer entry; captured operands, one header read, no side effects, error completion | BMC; abstract two-edge heap |
| `linker_wire_*` | Real Linker follows/swaps/freeing/tail relinking, final heap/canaries, exact free requests, active enqueue, changed caller operands | BMC; small modeled read-first heap and legal free service |
| `semantics_*` | Port layout/auxiliary views, copyability, all N32 operations including wrapping, erasure/enqueue, Interact annihilate/copy routing and faults | Packing/lookups inductive; execution paths BMC at service cutpoints |

## Environment and abstraction boundaries

The runner supplies a single resetless sync domain. All state begins at its
configuration-time initialization. Runtime reset assertion/release, CDC, FPGA
primitives after technology mapping, and physical timing are **not** verified.

Arbiter clients hold pending requests/payloads until acceptance. Fairness requires
a persistent requester and bounded resource readiness (at least every two
cycles). Safety/routing cases do not generally require eventual readiness.
Some client constraints refer to public grant signals to express this interface
contract; they do not assume the winning client or expected result.

Allocator scripts free only previously allocated, initialized words. Their heap
is an explicit reliable two-edge read-first SMT-array model, not the production
Heap. Heap behavior is checked separately. The longer allocator scripts use a
specified alternating backpressure pattern, **not arbitrary stall histories**.
Their application data, request counts, or free order are symbolic where stated
in the harness. Reserved prefixes and page budgets are intentionally small.

Semantic controllers use legal downstream service models. Interact tests use
the real dispatch program but abstract its leaf engines; they are not whole
Interactor proofs. Their post-acceptance live-input poison changes payloads but
keeps tags/labels fixed to specialize the selected dispatch cutpoint. Linker and
Grafter capture tests allow the entire caller payload to change after acceptance.
Selected operands are arbitrary constants, while ready/done schedules cover the
documented finite stall ranges. Linker wire tests abstract
pair/page metadata handling: frees update the modeled half-word marker only.
Memory observations used for induction are read-only and **asserted invariants**,
not assumptions that internal DUT state is correct.

This suite intentionally checks current fixed-latency and registered-arbitration
contracts. An optimization that changes a public latency/throughput contract may
require a deliberate harness update. Do not simply copy new DUT behavior into the
oracle, disable assertions, or constrain away a counterexample.

## Slow and unverified checks

```sh
python -m ivga.slop.verify --list --slow
python -m ivga.slop.verify --slow 'alloc_pair_*' --timeout 120
python -m ivga.slop.verify --slow 'linker_wire_follow_two'
```

The multi-pair bump, half-free, reuse, reclaim, reserved-OOM, and reachable-prefix
rollback checks exceeded the initial 30-second phase budget during development
(some during cover rather than safety checking). They remain in the suite as
`slow=True`, excluded from the default run. A timeout is **not** a proof or a
counterexample. They have not been certified by passing simulations instead.

The rollback script reaches its starting state through real allocations rather
than seeding allocator internals; it needs an 837-step bound with the actual
128-word pages. The slow two-slot wire-follow case has passed its full BMC/cover
run with a larger timeout, but is kept opt-in because it takes about a minute.

## Known failing specifications

```sh
python -m ivga.slop.verify --regressions 'semantics_regression_*'
```

These run as actual failures, not xfails. Currently they expose:

1. Commutation constructs B-side principal ports with A's tag/label.
2. Commutation does not report completion/update its completion statistic after
   its link requests finish.

The completion regression omits the earlier payload-identity check so the first
bug cannot mask the second. Passing the default suite does **not** certify
commutation. Production behavior is deliberately not changed by this test work.

## Remaining gaps

No whole-Runtime/Ivy equivalence proof, valid-graft interpreter proof, branch/call
continuation integration proof, general scheduler liveness proof, or arbitrary
multi-command allocator ownership proof is claimed. The existing randomized
allocator/extrinsic simulations and verified Fibonacci result remain useful
complementary checks. Future multi-core mutation safety, bank conflicts, queues,
and external DRAM protocols will need new contracts and properties.
