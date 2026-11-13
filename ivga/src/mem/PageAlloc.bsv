import GetPut::*;
import Assert::*;
import Heap::*;
import BRAM::*;

typedef struct {
    Bit#(40) value;
} PageId deriving (Bits, Eq, Ord, FShow);

function PageId pageId(Bit#(40) value);
    return PageId { value: value };
endfunction

function Addr pageIdToAddr(PageId page);
    return addr({page.value, 5'b0});
endfunction

PageId pageIdInvalid = pageId('1);

interface PageAlloc;
    method ActionValue#(PageId) alloc;
    method Action free(PageId page);

endinterface

module mkPageAlloc#(Heap heap)(PageAlloc);
    Reg#(PageId) nextPage <- mkReg(pageId(0));
    Reg#(PageId) freeHead <- mkReg(pageIdInvalid);
    Reg#(PageId) freeCount <- mkReg(pageId(0));

    method ActionValue#(PageId) alloc
        if (freeCount == pageId(0) && nextPage != pageIdInvalid); // TODO OOM

        nextPage.value <= nextPage.value + 1;
        return nextPage;
    endmethod

//     method ActionValue#(PageId) allocReuse
//         if (freeCount > 0 & nextPage != pageIdInvalid); // TODO OOM
//
//
//     endmethod

    method Action free(PageId page);
        dynamicAssert(page != pageIdInvalid, "free: cannot free invalid page");
        dynamicAssert(freeCount < pageIdInvalid, "free: freeing too many pages");

        // heap[addr(freeHead)] = freeHead
        // -> addr(freeHead) is always even
        // freeHead = page

        heap.even.portA.request.put(BRAMRequest {
            write: True,
            responseOnWrite: False,
            address: pageIdToAddr(freeHead),
            datain: word(zeroExtend(freeHead.value))
        });
        freeHead <= page;
        freeCount.value <= freeCount.value + 1;
    endmethod
endmodule
