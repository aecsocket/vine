# Links

- https://www.youtube.com/watch?v=d3nuepnbmC4
  - REALLY good explanatory video, made a lot of sense to me
- https://pages.hmc.edu/harris/ddca/ddca2e.html
- https://pages.hmc.edu/harris/ddca/ddcarv.html
- https://hdlbits.01xz.net/wiki/Main_Page
- https://nandland.com/lesson-1-what-is-an-fpga/
- https://blog.rhye.org/post/fpgas-for-software-engineers-0-basics/
  - https://x.com/ludwigABAP/status/1873080552821297222

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
