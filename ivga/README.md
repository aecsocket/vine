# Links

- https://www.youtube.com/watch?v=d3nuepnbmC4
  - REALLY good explanatory video, made a lot of sense to me
- https://pages.hmc.edu/harris/ddca/ddca2e.html
- https://pages.hmc.edu/harris/ddca/ddcarv.html
- https://hdlbits.01xz.net/wiki/Main_Page
- https://nandland.com/lesson-1-what-is-an-fpga/

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
