Easily write step-by-step logic in Amaranth which lowers to a finite state machine.

# Motivation

Take this Amaranth code:

```python
count_down_from: In(8)
done: Out(1)

m = Module()
counter = Signal(8)
m.d.sync += done.eq(False)

with m.FSM(init="init"):
  with m.State("init"):
    with m.If(count_down_from > 0):
      m.d.sync += counter.eq(count_down_from)
      m.next = "count_down"
  with m.State("count_down"):
    m.d.sync += counter.eq(counter - 1)
    with m.If(counter == 1):
      m.next = "finish"
  with m.State("finish"):
    m.d.sync += done.eq(True)
    m.next = "init"
```

If you're familiar with [coroutines] (e.g. [Rust futures]), you'll notice that we've just manually reimplemented a coroutine state machine! Why can't we get the machine to generate one for us?

```python
count_down_from: In(8)
done: Out(1)

m = Module()
m.d.sync += done.eq(False)

def program(f: amflow.Flow):
  with f.If(count_down_from > 0):
    counter = f.capture(count_down_from)
    
    with f.While(counter > 0):
      f.d.sync += counter.eq(counter - 1)
      yield

    f.d.sync += done.eq(True)
    yield

# `amflow.expand` expands to the same
# state machine that we've written above
amflow.expand(m, program)
```

The general idea is that, instead of writing an `m.FSM()` and using `m.State()`, you can write a generator function taking a `f: amflow.Flow` which `yield`s steps for the hardware to run. Each `yield` marks a place where a clock cycle happens, and where an FSM transition may happen. You can combine states with control flow like if/else/while to form more complicated programs.

```python
def program(f: Flow):
  f.m.d.sync += [
    some_other_value.eq(False),
    another_third_value.eq(False),
  ]
  
  with f.If(some_value > 0):
    f.d.sync += some_other_value.eq(True)
    yield
  with f.Else():
    f.d.sync += another_third_value.eq(True)
    yield
```

This can be used to "await" in hardware, like awaiting a memory read to chain it with a memory write:

```python
def program(
  f: Flow,
  read: amaranth.lib.memory.ReadPort,
  write: amaranth.lib.memory.WritePort
):
  a = yield memory_read(f, read, addr=4)
  b = yield memory_read(f, read, addr=5)
  c = a + b
  yield memory_write(f, write, addr=6, data=c)
```

One `yield` indicates that we've finished a _group of operations_, which will take at least one clock cycle, and may lead to a state transition.

```python
def program(
  f: Flow,
  # read from two separate memory modules
  read_a: amaranth.lib.memory.ReadPort,
  read_b: amaranth.lib.memory.ReadPort,
  write: amaranth.lib.memory.WritePort
):
  # these two ops get done in the same
  # state transition (and, here, clock cycle)
  a = memory_read(f, read_a, addr=0)
  b = yield memory_read(f, read_b, addr=0)

  # ...and their results are available 1 cycle later.
  # write into a third disjoint memory module
  c = a + b
  yield memory_write(f, write, addr=0, data=c)
```

Keep in mind that all of this exists _on top of_ Amaranth, so you get all the benefits of the Amaranth ecosystem, and can freely mix `amflow.expand` and regular `Module` elaboration functions.

```python
class MyModule(wiring.Component):
  def elaborate(self, platform):
    m = Module()
  
    with m.If(self.do_one_thing):
      expand(m, lambda f: program(f, self))
    with m.Else():
      m.d.sync += self.output.eq(True)

    return m

def program(f: Flow, my_module: MyModule):
  yield
```

[coroutines]: https://en.wikipedia.org/wiki/Coroutine
[Rust futures]: https://doc.rust-lang.org/book/ch17-01-futures-and-syntax.html
