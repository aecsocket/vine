import GetPut::*;
import Assert::*;
import Heap::*;
import BRAM::*;
import FIFOF::*;

typedef struct {
    Bit#(40) value;
} PageId deriving (Bits, Eq, Ord, FShow);

function PageId pageId(Bit#(40) value);
    return PageId { value: value };
endfunction

function PairAddr pageIdToPairAddr(PageId page);
    return pairAddr({page.value, 4'b0});
endfunction

PageId pageIdInvalid = pageId('1);

interface PageAlloc;
    method Action alloc;
    method ActionValue#(PageId) allocPage;

    method Action free(PageId page);

endinterface

typedef enum {
    Idle,
    ReadWait,
    ReadDone
} AllocState deriving (Bits, Eq);

module mkPageAlloc#(Heap heap)(PageAlloc);
    Reg#(PageId) nextPage <- mkReg(pageId(0));
    Reg#(PageId) freeHead <- mkReg(pageIdInvalid);
    Reg#(PageId) freeCount <- mkReg(pageId(0));

    Reg#(AllocState) allocState <- mkReg(Idle);
    FIFOF#(PageId) allocPageOut <- mkSizedFIFOF(4);

    rule allocDone (allocState == ReadWait);
        let nextPage <- heap.even.portA.response.get;
        allocPageOut.enq(freeHead);
        freeHead <= pageId(truncate(nextPage.value));
        freeCount <= pageId(freeCount.value - 1);
        allocState <= Idle;
    endrule

    method Action alloc if (allocState == Idle);
        if (freeCount == pageId(0)) begin
            // bump allocate
            dynamicAssert(nextPage != pageIdInvalid, "alloc: OOM"); // TODO

            nextPage <= pageId(nextPage.value + 1);
            allocPageOut.enq(nextPage);
        end else begin
            // reallocate from free list
            // - nextPage <- heap[addr(freeHead)]
            // - freeHead <= nextPage; return old `freeHead`
            dynamicAssert(freeHead != pageIdInvalid, "alloc: free head is invalid"); // TODO

            heap.even.portA.request.put(BRAMRequest {
                write: False,
                responseOnWrite: False,
                address: pageIdToPairAddr(freeHead),
                datain: word(0)
            });
            allocState <= ReadWait;
        end
    endmethod

    method ActionValue#(PageId) allocPage;
        let page = allocPageOut.first;
        allocPageOut.deq;
        return page;
    endmethod

    method Action free(PageId page) if (allocState == Idle);
        dynamicAssert(page != pageIdInvalid, "free: cannot free invalid page");
        dynamicAssert(freeCount < pageIdInvalid, "free: freeing too many pages");

        // - heap[addr(freeHead)] <= freeHead
        //   addr(freeHead) is always even
        // - freeHead = page

        heap.even.portA.request.put(BRAMRequest {
            write: True,
            responseOnWrite: False,
            address: pageIdToPairAddr(page),
            datain: word(zeroExtend(freeHead.value))
        });
        freeHead <= page;
        freeCount <= pageId(freeCount.value + 1);
    endmethod
endmodule
