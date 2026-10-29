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
