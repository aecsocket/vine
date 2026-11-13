package Top;

import Heap::*;
import PageAlloc::*;

interface Leds;
    (* always_ready, result = "led0" *)
    method Bool led0;
endinterface

(* synthesize *)
module mkTop(Leds);
    Heap heap <- mkHeap;
    PageAlloc pages <- mkPageAlloc(heap);
    Reg#(Bool) ledOn <- mkReg(False);
    Reg#(Bit#(2)) state <- mkReg(0);
    Reg#(PageId) lastPage <- mkReg(pageId(0));

    rule tick0 (state == 0);
        pages.alloc;
        state <= 1;
    endrule
    rule tick1 (state == 1);
        let page <- pages.allocPage;
        lastPage <= page;
        $display("page alloc'd = %h", page);
        state <= 2;
    endrule
    rule tick2 (state == 2);
        pages.free(lastPage);
        state <= 3;
    endrule
    rule tick3 (state == 3);
        state <= 0;
    endrule

    method Bool led0;
        return ledOn;
    endmethod
endmodule

endpackage
