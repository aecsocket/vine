# Links

- https://www.youtube.com/watch?v=d3nuepnbmC4
  - REALLY good explanatory video, made a lot of sense to me
- https://pages.hmc.edu/harris/ddca/ddca2e.html
- https://pages.hmc.edu/harris/ddca/ddcarv.html
- https://hdlbits.01xz.net/wiki/Main_Page
- https://nandland.com/lesson-1-what-is-an-fpga/
- https://blog.rhye.org/post/fpgas-for-software-engineers-0-basics/
  - https://x.com/ludwigABAP/status/1873080552821297222
- https://fpgacpu.ca/fpga/outline.html

## nandland

starting with this.

https://nandland.com/lesson-7-what-every-software-programmer-needs-to-understand-about-hardware-design/

state machines - I get it. it's like rust futures, or coroutines more generally.

learning about FIFO queues https://nandland.com/lesson-8-what-is-a-fifo/ - I get this. it's basically just rust MPSCs. and you can't write to a full queue.. yeah they're isomorphic! depth of a FIFO = channel capacity. width of a FIFO = the datatype `T` of the `mpsc::Sender/Receiver<T>`. except in FPGA the data is always binary, wires, so the depth is expressed in bits.

this has me thinking, how could you use them? crossing clock domains.. perhaps for parallelism between cores executing the IVM reducdtion?

> FIFOs are one of the basic building blocks for FPGA designers and are critically important to understand and use correctly!

well yea like rust concurrency lol

metastability: this reads like data races in computers. it seems so analogous.

ok done reading, it seems simple enough but I need some hands on practice.

## HDLBits

got partway thru. I get the basic idea of writing verilog. want some proper practice tho.

# Building

## 21 Oct

got a basic BRAM working. want to get interactions running now. I think the simplest way is to make an interact.sv, have it take 2 words, and the system memory, then work with that. somehow? idk.

ok got heap memory, and memory allocator working. fairly simple bump alloc + free list nothing too hard. now port linking is a pain in the ass and complicated.

I just had a thought. for any real applications, you will need at some point to store big ass blobs in memory. now, vine doesnt represent that very well. you can have lists (head + tail), or arrays, but these all have a not-insignificant amount of memory overhead. could we do better? what if, at the hardware (or IVM) level, you have an extrinsic to freeze a byte array. this is stored and is immutable at the hardware/machine level. and you get back a handle to that data. and you can do random indexing into that data. like a byte array. so its like an `Arc<[u8]>` essentially, but at the hardware level. and its interesting, because it kinda fits nicely with the memory management story of vine/ivy! your nodes are all auto-garbage collected, cause of the nature of the language, but some stuff you DO want to share, like binary blobs or pngs or whatever. that gets ref-count garbage collected. when the last handle is erased in the net, then the hardware will actually free up all that memory. that kinda fucks up the freelist idea tho, imagine freeing a 2GB file and you have to do 2 billion free operations on the free list... fuuck that would suck. hmmmmm. much to think about!

## 22 Oct

apparently that idea I had last night, they already thought about shared immutable state in a vine PR. good that I came up with something similar independently I guess!

I decided I didn't like verilog, and I knew about Amaranth I saw a guy on youtube using it, so I was like what the hell let's try it. I gotta say it's a lot more readable, and it takes care of a lot more of the edge cases/reset/clock/etc. stuff, so I'm sticking with this. its very nice.

I got the heap/alloc/link (partially) reimplemented in Amaranth, but I'm worried about testing. I know I have testbenches but thats lowk not enough. I started to look into formal verification instead, I think I can get AI to autogenerate a bunch of formal verification test cases for my alloc module.

I GOT THE LINKER WORKING! I had no clue what the old code was doing, so I sat down, made a new file, and decided to oneshot the new impl myself. And it worked almost perfectly on the first try! I just forgot to check if wire A == wire B.

Now for the worklist.. I think I need a stack for this. How do we handle worklist stack overflows? Shit... uh idk. We need maybe "CPU registers" or fault registers so if we overflow we stop all computation? And hard reset? Idk really.

This gets me thinking... CPUs have faults and fault handlers. So you can go: get a segmentation fault, and it goes into the interrupt descriptor table (IDT), finds the kernel's IDT entry for a segfault, and runs that code. if *that* interrupt segfaults, then it triple faults and the system hard resets. Could we have interrupt handlers in our interaction net hardware? is that a good idea? Maybe if you store a port/net in a table? I don't really know... Could use it for worklist overflow and for out of memory?

the grafting scares me. external IO scares me.

## 23 Oct

got my arty a7 artix-7 100T today, been trying to program it. seems simple enough, had to install vivado which eh whatever.

implemented alloc/free statistics hardware registers, want to add them for more stuff like erases/combs/grafts/etc. and output them to a host so I can see more detailed info.
one thing thats really interesting is how like, I actually understand what a CPU register is physically now. its actually a piece of hardware on the CPU which electrically adds when like the timestamp counter increments or an instruction is performed. i know thats obvious thats literally comp sci 101 but its like, oh *that's* how it works in silicon.

hmm for the other hardware counters, like the number of erases, we have a little issue. we increment it in 2 places: the link module and the interact module. (tbf maybe they should just be the same module? im not sure tbh. i think maybe yeah. idk.)
anyways assuming we keep it split, then the link needs to add a count, and the interact needs to add an erase count. we can do this by either:
- arbitrating a request between the 2 to increment count
  - thats lowk dumb we dont want to stop the fsm because of waiting for a counter req
- storing TWO counters, one for the interact and one for the linker, then the final `stat_erase` is combinatorially eq to `link.stat_erase + interact.stat_erase`

i think the 2nd approach makes more sense, but we have to dedicate another 64 bits for the counter. (or acc we can use smaller registers)

WAIT i actually might be completely wrong. linking only erases, interactions may annihilate. just read the IVM source, yes that looks right. ok so we dont have to worry about this yippee. I confused erasure for annihilation

## 28 Oct

Working on the rest of the interactions in the interactor. do we *really* have to wait 2 times for the linker? what if we had 2 linker units for erase/copy? or 4 for commutation? I know we still have competition on the heap/alloc arbiter, but..? Something to experiment with. Might speed up the interact engine to do things in less cycles.

what about vectorized/wide allocations?

ok one thing that REALLY annoys me about this design is the fact that we can only do one memory read/write at a time, and only one-word alloc/frees at a time. this seems lowkey ridiculous and this has gotta be something I can speed up.
the bump allocator + freelist architecture _really_ has gotta be improved bro. Our fundamental limitation is 1 read + 1 write per clock cycle; but maybe allocating in pages of 2 words? or pages of 4 words? but that is probably TERRIBLE for fragmentation.
also how are we eventually gonna handle frozen immutable shared memory like that vine proposal I talked about on 21st? cause I'm sure as hell not doing 2 billion pointer chases to allocate 2gb of memory from our free list. and its unreasonable to use 2 separate heaps, one for the net and one for immutable shared data. (or is it? my gut tells me this is a really bad idea cause RAM is already scarce enough.)

Anyway that's thinking for another time, but rn I need to make the allocator vector-aware anyway, because implementing commutation without vectorized alloc is dumb. You're telling me I need FOUR FSM states to represent the allocations a commutation does? That's dumb the allocator should just be vector aware instead, even if behind the scenes its still N allocs = N cycles.

Going back to an earlier design decision rn. The interactor is wired to a link unit externally, because I assumed that smth else needs to use the linker. But now looking at it more, the interactor really needs more than 1 link unit, otherwise it has to go through 8 (!) FSM states to do a commutation. That's also not great... what if I just gave the interactor 8 link units? We can't speed up memory access obviously but we CAN do all links as one big batch which we await much easier. If we do that, then where else is linking used? Looking at the IVM:
- in the interactor (duh)
- in grafting (could reuse the interactor's?)
- in the IVM runtime setup - linking the IO port up

This is a very small inventory of callsites... we can give the interactor its own 8 linker submodules, then we instantiate 1 extra linker for runtime setup that remains mostly unused. Then the interactor has to arbitrate memory access tho. That's.. less than ideal but we would need to arbitrate memory access anyway. And of course we can play around with this later - if we need to reduce silicon space/LUT usage, we can give an interact unit 4 linker units + split commutation into 2 FSM states. Or 2 LUs + 4 states.

Ok I think I managed to implement arbiters in a very clean and simple way. We have a generic `Arbiter` which just does the round-robin prio logic, takes in a `request` and outputs a `grant` bitfield. Then we have `AllocArbiter`, `HeapArbiter` etc. which just use that and when a client wins a request, we `wiring.connect` the two up. The `*Arbiter` classes are all almost exactly the same, apart from what constitutes a "client wants a request" signal, which is pretty cool. I'm no expert but I think this is a clean implementation.

nevermind my arbiter transactional model is wrong FUUCKKKK

## 29 Oct

GPT linked me this https://fpgacpu.ca/fpga/outline.html - looks useful.

Okay, it's getting kinda impossible to write more Amaranth code without addressing something important. *Everywhere* in this code I'm writing like, request alloc -> in the next FSM state, hold alloc high + wait for grant -> in the next FSM state, hold low and wait for done. Repeatedly. And this is happening with alloc, with heap, with stack, everything. it's REALLY hard to follow the actual goddamn control flow. I need to spend some time designing some kind of way to generate this code at a higher level, otherwise I'm gonna go insane.

## 31 Oct

I've still been grinding away at amflow. I got amflow working with control flow and stuff as well, mostly AI coded but I did the design, because I cba to figure out the details of how Amaranth and Python work. I'm here to learn hardware.

Anyways, I got the heap, allocator, stack, linker, interactor all rewritten with the new amflow API and it's SO much more readable!! You can actually track control flow with `yield`s and `f.If` and `f.While` etc, it's amazing.

To implement commutation, I have to implement something new in the allocator. 2-word allocations and frees. Otherwise, I can't guarantee that `word ^ 1` points to a port; the free list stores individual freed *words*, not *word pairs*, so `word` might be a valid allocation I get from the allocator, but `word ^ 1` not necessarily. I want to match the IVM here, which allocates in pairs of words, and frees in individual words, but only finishes the free once BOTH words (both *halves* of a *pair*) are freed.

My AI raised something interesting. The IVM uses the heap to track allocator bookkeeping - when a word is freed, we write `!0b111 = 0b1111_1111_[...]_1000` into it - this is a `Tag.INVALID` with all-ones payload and label. AI proposed that we have a `list[pending]` bitfield, which takes `capacity_in_words / 2` bits of extra FPGA memory. That is, if I have a heap of 1KiB/1024 words (64KiB of memory), I need 0.5KiB (512B) of extra DRAM to track which pairs are freed. For a heap of 256MiB, that's 512KiB of memory used for allocator accounting. This lets us avoid a heap write, but I *really* don't like that extra memory usage. I think we have to go with the heap write here; we can't waste this memory, and also we'll eventually have to touch the heap anyway.

Also in theory I can make the allocator work on "pages" instead of "pairs" (or rather, a pair is just the case where page size = 2 words), but theres not much point. But just interesting to note.

type safety thought: rn our heap works on `Word`s, and it can't work on `Port`s directly because the heap writes arbitrary words in, when it writes addresses for the free list. this means that reading a value from the heap isn't guaranteed to be a Port. but it would be nice if it did? cause then you dont have to cast between words and ports.

now the thing is, a Word is 64 bits, and the allocator writes 45-bit Addrs in. a Word can *store* an Addr - Wires store Addrs in their `payload`! so what if, instead of the Alloc writing Addrs into the heap, they wrote `Port(tag: Tag.NONE, payload: <addr payload>, label: 0)`?

actually I just implemented the above ^^ was pretty easy.

## 1 Nov

I'm working on a big scary part now - GRAFTING. Why is grafting so complicated here? Up until this point we've been working under the assumption that all initial ports were specified in memory/heap somewhere. With grafts that calculus changes, and now a `Port(Tag.GRAFT, payload: 0x1234, label: 0)` points to a _graft struct_ at index `0x1234`. That graft struct is NOT a `Port` or a `Word` - it's a `Graft`, which I assume would look like:
- `heap[base][30:0] = next_register: Register`
- `heap[base][31:31] = error: bool`
- `heap[base][64:32] = instructions_len: u32`

That mirrors the Rust `struct Graft`. So actually, wait... a whole `Graft` could fit in a single 64-bit word. Use the lower 31 bits for `next_register`, the 32nd bit for `error`, and the upper 32 bits for `instructions_len`; then the instructions data is stored contiguously after `heap[base]`. This assumes that we ALWAYS have a variable-sized contiguous allocation after `heap[base]` - we can assume this on program startup, yes, but _not necessarily_ if/when we load an arbitrary program.

This is an important point to note too. I'm planning for the hardware to be able to load arbitrary programs - so from an existing FlatNet it can put arbitrary programs into its heap, which means arbitrary Grafts. Which means we _will_ need to de/allocate grafts in the heap.

This brings me onto what I just thought about... the free list. That currently uses a separate bit of DRAM to store its stack, but really it shouldn't be, it should use our available global memory. Or should it? I mean, I don't know how big an active list might get, and in theory it could be huge, so we should allow tucking it all away in DRAM if we need to.

Back to grafts... that whole thing about contiguous allocation. Once we introduce the idea of de/allocating programs and grafts, we like don't have a guarantee of that at all. Which means we probably need an MMU... and a TLB... ok this is getting scary. WAIT maybe we don't need to! The graft can be a chain of allocations, and we don't need contiguous memory, just store the address of the next allocation at the end. OK crisis averted no need for an MMU (yet).

Annoyingly this means I have to go back on the thing I did yesterday where `Heap` works on `Port`s not `Word`s.

## 2 Nov

I actually got the whole engine working on a real board - I got AI to make the UART statistics output thing because I literally cba at that point, but it's UART it's not like it's particularly complicated so I can recreate it myself if need be. not like there aren't resources on implementing UART tx/rx on an FPGA online.

Ok this is kinda ridiculous, my design was sitting at -18ns WNS which is TERRIBLE, and it was like 2am so I just kept prompting to get it down to 0.076ns WNS. It looks like a `yield` bomb went off in my codebase, but basically my AI added yields everywhere to add clock cycles; flattened the arbiters to avoid some combs there; and added an output register to heap memory, adding an extra clock cycle between req and resp BUT improving slack. I need to go over all of the changes and unfuck it but hey at least it works now, and I know that it *can* work on a real board.

anyway, now the hard part is extending this. Adding a DRAM controller so I can talk to the 256MiB of onboard DDR3 RAM, pipelining the memory ops and interact ops to improve throughput. I *thiiink* that we know the latency upper bound of all ops on our board (like allocs, links, interacts), so we should be able to design perfect pipelining, I think? perfect pipelining as in under perfect conditions there arent any bubbles in the pipeline. I have to think about it a bit more, but I get the general idea of how to make this work.

Also while talking about the memory design I realised something - our Heap is 1 memory port which is like the least throughput you can get, but we *can* synthesize multiple memory banks, each with their own rw ports. That would improve our throughput massively if we can use it properly, and reduce num of cycles needed for a lot of stuff. In particular, the pair alloc -> it always allocs 2 words from the memory block, so why dont we give it 2 memory blocks/2 read ports, so it can do them in parallel? We have one block/bank for odd allocs, and one block/bank for even. Mux the read port based on the LSB - 0 = even alloc bank, 1 = odd alloc bank. Helps with pair alloc; idk about page alloc. Oh that reminds me we also have to move the worklist stack into the heap. we have the page allocator now so should be a lot easier.

So, also if we talk to onboard DRAM then like, we WILL need to handle caching. Thankfully we don't need MESI cache coherence bullshit (thats like one of the whole reasons this arch is good), but we still need to say like, oh we're accessing an address that's not in our cache line, we need to fetch some stuff into memory. Not a cache line necessarily though - we can't exploit memory locality, since there's no guarantee that the next ops will be nearby in RAM. Instead, we can exploit the *worklist!!!!* We have a literal list of "what work are we gonna be doing next" - that is essentially our list of "what addresses need to be in cache soon?" So we can implement a hardware prefetcher which isn't heuristicslop! I don't have the exact cache design in my head rn, but I think I can figure this out. This is totally doable.

Also when I start reimpl'ing stuff, then I will need to take WNS into account a lot more. Cause obviously this caused problems for me lol.

One thing I really need to start thinking about: how will the program load another program which it hasn't been programmed with?

## 7 Nov

Investigating shared mutable state in Vine. I wrote a dummy http server `http.vi` which has a `num_visits: N32` as shared global state among all route handlers. It accepts a tcp stream, makes an `io.split() = io2`, uses that `io2` for the task, and pushes `io2` onto a list. after the `while io.forever()` we merge all the `io2`s into the original `io` - this is essentially fork/join model. Now, the interesting part is that `num_visits` - each `handle_stream(io2, &num_visits`) gets a ref to that. But that's BAD - it means that every stream handle task is serialized on `num_visits`. even tho we split the IO, we still need to finish `handle_stream` to know what the new value of `num_visits` will be. So we've unintentionally serialized all our tasks.

My initial solution was to make a `Mutex` extrinsic which makes a critical section by blocking the thread - its literally just a wrapper around a rust mutex nothing fancy. But, obviously this blocks the worker, which is bad. Instead I was pointed to the `atomic` branch which uses a new type called `#root::Atomic`. Here's how it works:

```rs
// make a new atomic counter
let counter = Atomic::new[N32](0);

// use the counter
counter.access((), fn((), &count: &N32) {
  count += 1;
});
```

Explanation of the syntax:
- `.access((), ..)` - the `()` is what this access *depends on*.

  So, in order to access `counter`, we must first wait for `()` to be available to the IVM - that is, we *await* `()`. However, `()` is trivially awaitable - it's always ready. So this is essentially: for any `t: Atomic[T]`, `t.access((), f)` is equivalent to `f(the value under t)`.

- `.access(.., fn((), ..))` the `()` in the `fn` sig is the same context type passed in as the first argument to `access`.

  So this is basically saying, "once you're inside this `fn` body, *both* the context you wanted and the atomic counter are available*.

This kinda sounds like a coroutine, but I think that's the wrong mental model here. Since the IVM doesn't really have the concept of tasks or awaiting.

In the internal machinery, await looks like this:

```rs
pub trait[T] Await {
  fn _await[F; fn F(T)](value: T, f: F);
}

pub impl tuple_nil: Await[()] {
  fn _await[F; fn F(())]((), f: F) {
    f(())
  }
}

pub impl n32: Await[N32] {
  fn _await[F; fn F(N32)](value: N32, f: F) {
    if value == 0 {
      f(value)
    } else {
      f(value)
    }
  }
}
```

Basically the way to read this: `Await` lets you say, "if `Await[T]` (basically rust `T: Await`), wait for `t: T` to resolve to a real, actual value in the machine, then run `f(t)`".

Now, from a sequential programming model, this kinda doesn't make sense. If you call a function `some_val.do_await(|the_same_val| {})`, then, like... you already *have* `some_val` to call `do_await` on it! Why the hell would you need to access it in a closure? You literally *have it in your CPU register* (or your stack or wherever). In the IVM, this isn't the case! Saying `let x = some_val.do_some_fn()` just says, "connect the place `x` to the result of `some_val.do_some_fn()` - but I have no clue when that'll happen". But because we're more describing a *graph of dependencies* here, then it's not guaranteed that `some_val` is actually available at this point in the code flow.

For example, take this code:
```rs
fn foo() {
  let x = 6 + 7;
  let y = do_a_computation();
}
```

In a traditional CPU, this would emit assembly like:
```asm
mov r1, 6
mov r2, 7
add r3, r1, r2 ; register r3 is used for x

jmp do_a_computation
mov r4, ret_val ; register r4 is used for y
```

Written like this, this implies a sequential dependency between r3 and r4, because assembly and machine code represents a step-by-step series of instructions to do. Obviously there isn't a dependency between r3 and r4 here though; and modern CPUs know this. That's why they do out-of-order execution and try to figure out on the fly whether r3 and r4 depend on each other, and if not they can reorder these if it makes more sense for performance.

However, the compiler *already knows this data dependency!* We shouldn't have to force the CPU to do heuristics to figure this out; we already *knew this when we wrote the damn machine code!* So Vine would see the above code, and build a net that instead looks like:
- wire `x` is the result of `vi:n32:add(ivm:n32#6 ivm:n32#7)`
- wire `y` is the result of `ivm:graft[:do_a_computation]`

Note that this is explicitly **not** a list of instructions to do step-by-step. It just tells the computer, "if you ever want to figure out what `x` is, then do `vi:n32:add(..)`"; NOT "add 6 and 7, then put it into x". It's always possible that we just never use `x`:

```rs
fn foo() {
  let x = 6 + 7;
}
```

`foo` doesn't output anything, it's entirely pure, so the whole `6 + 7` computation is wasted, so the computer will never even ask what `x` resolves to; nothing ever wants to know what `x` is.

Anyway, how does this relate to `Await`? Imagine we have this code:
```rs
let counter: N32 = 0;

let do_an_expensive_calc = fn(&io: &IO) { io.sleep(1000); 1337 };
let expensive_value: N32 = do_an_expensive_calc(&io);

counter += expensive_value;
```

We want to add `expensive_value` (which we only know after a really complex computation) to `count`, and the code above perfectly encodes the dependency chain here:

- the final value of `counter`
  - ..depends on the current value of `counter` and `expensive_value`
  - ..`counter` is already 0 as we know
  - ..`expensive_value` depends on `io.sleep(1000)` finishing

Everything is cool and awesome.

Now take this code:
```rs
fn main(&io: &IO) {
  let tcp_listener = open_http_server(&io);
  while io.forever() {
    let tcp_stream = tcp_listener.accept(&io);
    let io2 = io.split();
    handle_http_request(&io2, tcp_stream);
  }
}

fn handle_http_request(&io: &IO, tcp_stream: &TcpStream) {
  let some_value = do_db_ops(&io);
  tcp_stream.write_html("<h1>val = {some_value}</h1>");
}

fn do_db_ops(&io: &IO) -> N32 {
  io.sleep(1000);
  1337
}
```

Here, our goal is to listen for incoming HTTP requests, then do some expensive processing based on that request (1 second!), then return the result to the caller. As written, this code works perfectly - because notice the dependency chain:
- `tcp_stream` depends on `tcp_listener.accept(&io)`, which depends on `&io`
- where do we get `io` from?
  - on the first iteration, it comes from the `io` handle that we got given in `main`
  - on subsequent iterations, it comes from doing the `io.split()` call - remember that `io2 = io.split()` desugars to `(io, io2) = split(io)`, so we need `io.split()` to finish before we get the `io` for the next `io.forever()` iteration
- `handle_http_request` only depends on `io2`, which is fully independent of `io` after the `io.split()` call
  - therefore, **we don't have to wait for `handle_http_request` to finish, to process the next `io.forever()` iteration!**

This is INCREDIBLY POWERFUL!!! In Rust, you'd have to do `std::thread::spawn(|| handle_http_request(..))` or `tokio::spawn(|handle_http_request(..))` - in Vine this is **entirely unnecessary** due to how dependencies, concurrency, and parallelism works. You just *say what you want to happen*, rather than *how you want the OS/task runtime to do it* (`tokio::spawn` etc.)

BUT - there is a problem with this approach. Consider this:
```rs
fn main(&io: &IO) {
  let num_visits: N32 = 0; // <-- new!
  
  let tcp_listener = open_http_server(&io);
  while io.forever() {
    let tcp_stream = tcp_listener.accept(&io);
    let io2 = io.split();
    handle_http_request(&io2, &num_visits, tcp_stream);
  }
}

fn handle_http_request(&io: &IO, &num_visits: &N32 tcp_stream: &TcpStream) {
  let some_value = do_db_ops(&io);
  num_visits += 1; // <-- new!
  tcp_stream.write_html("<h1>val = {some_value}, you are visitor number {num_visits}</h1>");
}

fn do_db_ops(&io: &IO) -> N32 {
  io.sleep(1000);
  1337
}
```

We've added a `num_visits` - each HTTP request will do expensive database ops, then increment the visits counter (or in the opposite order; doesn't matter for this example). Now notice what happens with our dependency chain:
- we need `io` for the `io2 = io.split()`, and for the `handle_http_request(io2, ..)`
  - on the first iter, we get it from `main(&io)`
  - on subsequent iters, we get it from the `io` left over after `io.split()`
  - everything is OK like before
- we *also* need the latest value of `num_visits`; where do we get that from?
  - on the first iter, it comes from `let num_visits: N32 = 0` - no problems here
  - on subsequent iters, we **have to wait** until `handle_http_request` is done with the `num_visits` counter - this means we **have to wait** for `do_db_ops`
  - this means that, to start the next `io.forever()` iteration, we have to have `num_visits` available, which is **only available after the last `handle_http_request` is done**

We have accidentally serialized our whole HTTP server!

Well, shit. What do we do?

`Atomic[T]` of course!

```rs
fn main(&io: &IO) {
  let num_visits = Atomic::new[&N32](&0); // <-- new!
  
  let tcp_listener = open_http_server(&io);
  while io.forever() {
    let tcp_stream = tcp_listener.accept(&io);
    let io2 = io.split();
    handle_http_request(&io2, num_visits.fork(), tcp_stream);
  }
}

fn handle_http_request(&io: &IO, num_visits: Atomic[N32], tcp_stream: &TcpStream) {
  let some_value = do_db_ops(&io);

  // new!
  let num_visits_now = num_visits.access((), fn((), &num: &N32) {
    num += 1;
    num_visits_now
  });
  
  tcp_stream.write_html("<h1>val = {some_value}, you are visitor number {num_visits_now}</h1>");
}

fn do_db_ops(&io: &IO) -> N32 {
  io.sleep(1000);
  1337
}
```

Now our dependency chain has changed:
- to do the next iteration of `io.forever()`, we require `io` and `num_visits` to be available
  - `io` is fine, as discussed before
  - `num_visits` is never consumed or referenced by `handle_http_request` - instead, we make a *copy* of `num_visits` (`num_visits.fork()`), and pass that copy into the function
  - this leaves the original `num_visits` immediately accessible for the next iteration

So we've solved the "don't accidentally serialize requests" part. What about actually using the counter? How do you get the `N32` out of an `Atomic[N32]`?

That's where `Atomic[T]::access` comes in - you give it 2 things:
- a context `C`, in our case `()`
- a function `fn(C, T)` to run **once the context and atomic value are available**

The semantics of this operation is: once the context and the value are ready to use, *then* we run the `fn()` which increments `num += 1`, and gives us back the new `num_visits_now`. So it gives us a way to sorta "join" on two values being available, then do an op on it.

Anything inside the function is effectively the critical section of this atomic - no other work can be done on this `num_visits` atomic while the function is being reduced to normal form. Once the `num_visits_now` (the final value) has been computed, the underlying atomic value is now available again, and the condition ("both the context and value are available") is satisfied for the next caller.

Why the context? Well, our `access` might require something from the environment:

```rs
let how_much_we_increment_num_visits_by = do_db_ops(&io);

// new!
let num_visits_now = num_visits.access(
  how_much_we_increment_num_visits_by,
  fn(hmwinvb, &num: &N32) {
    num += hmwinvb;
    num_visits_now
  }
);
```

This gives us a way to explicitly say, "wait for the db op to finish, *then* enter the critical section".

So we don't even need any mutexes or blocking to express this - all of this can be done with regular IVM constructions like ports and wires! How? I still don't fully understand this part, but the basic idea is that we add the critical section work to the worklist *only once its deps are satisfied*; and to satisfy the 2nd dep (the "value is available" dep), that can only be done *once the previous caller has exited the critical section*, and has returned the `value` to its original place.

I forgot to mention how this ties into the `if value == 0 { f() } else { f() }` stuff. That is admittedly really weird code, but there's a simple reason for it:
- `value` is an extrinsic value (it's a `vi:n32`).
- There isn't a way to inspect an extrinsic value, apart from through extrinsic calls (like `vi:n32:eq`, which is what `value == 0` desguars to).
- We want to only evaluate `f()` once `value` is ready - the `Atomic` internally does `context.await(..)`, so `value` is the `context` here.
- Therefore, to establish this "wait-until" relationship, we must depend on *inspection* of the extrinsic value, which would require the IVM to resolve it to a real value.
- In the case of `vi:n32`, we do this via a `vi:n32:eq` call.

---

I've been working on changing the atomics API from:
```rs
let x = Atomic::new(0);
let y = x.access(some_calc, fn(some_calc, &x: &N32) {
  x += some_calc;
  x
});
io.println("{y}");
```

to
```rs
let x = Atomic::new(0);
let y = do {
  let x = x.access(&some_calc);
  x += some_calc;
  x
};
io.println("{y}");
```

which I believe is a more ergonomic solution. I've figured out how to solve it, and it's like stupidly simple lol.

First I played around with changing the `Await` machinery from:
```rs
pub trait[T] Await {
  fn _await[F; fn F(T)](value: T, f: F);
}
```

to
```rs
pub trait[T] Await {
  fn await(value: T) -> T;
}
```

This still lets us do things like:
```rs
pub impl n32: Await[N32] {
  fn await(value: N32) -> N32 {
    if value == 0 {
      value
    } else {
      value
    }
  }
}
```

So we can express the "depends-on-observing-the-value" relationship here, but this *doesn't* let us combine this dependency with something else to express a "depends-on-two-things":

```rs
pub fn .access[T, C; Await[C]](&Atomic[T](&token), &ctx: &C) -> &T {
  // awesome, we can await on `ctx`!
  ctx = ctx.await();
  // ..but this access doesn't depend on `ctx` at all. :(
  // and we can't make it do so. :(
  token.access()
}
```

So instead, I reverted the `Await` machinery to how it already is, and instead looked at changing `.access`. The solution turned out to be stupidly simple.

We went from the following:
```rs
pub fn .access[T, C, F, R; Await[C], fn F(C, &T) -> R](&Atomic[T](&token), ctx: C, f: F) -> R {
  let output;
  ctx.await(fn (ctx) { output = f(ctx, token.access()); })
  output
}
```

to:
```rs
pub fn .access[T, C; Await[C]](&Atomic[T](&token), &ctx: &C) -> &T {
  let output;
  let new_ctx;
  ctx.await(fn (ctx) {
    output = token.access();
    new_ctx = ctx;
  });
  ctx = new_ctx;
  output
}
```

When we enter the `await` closure, we can just assign the `&T` we get to a variable *outside of that closure*, and then *return that assigned value*. That way, the caller can get the real `&T` out; the "depends-on-two-things" relationship is preserved; and the critical section is preserved, since the critical section only ends once the final value is put back into `output: &T`.

My HTTP example seems to work the same with both the original API and my version of the API.
