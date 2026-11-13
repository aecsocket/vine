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
    Reg#(Bit#(8)) state <- mkReg(0);
    Reg#(PageId) lastPage1 <- mkReg(pageId(0));
    Reg#(PageId) lastPage2 <- mkReg(pageId(0));
    Reg#(PageId) lastPage3 <- mkReg(pageId(0));

    rule tick0 (state == 0);
        pages.alloc;
        state <= 1;
    endrule
    rule tick1 (state == 1);
        let page <- pages.allocPage;
        lastPage1 <= page;
        $display("[1] page alloc = %h", page);

        pages.alloc;
        state <= 2;
    endrule
    rule tick2 (state == 2);
        let page <- pages.allocPage;
        lastPage2 <= page;
        $display("[2] page alloc = %h", page);

        pages.alloc;
        state <= 3;
    endrule
    rule tick3 (state == 3);
        let page <- pages.allocPage;
        lastPage3 <= page;
        $display("[3] page alloc = %h", page);
        state <= 4;
    endrule
    rule tick4 (state == 4);
        pages.free(lastPage3);
        state <= 5;
    endrule
    rule tick5 (state == 5);
        pages.free(lastPage1);
        state <= 6;
    endrule
    rule tick6 (state == 6);
        pages.free(lastPage2);
        state <= 0;
    endrule

    method Bool led0;
        return ledOn;
    endmethod
endmodule

endpackage
