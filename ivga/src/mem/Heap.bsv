import BRAM::*;

typedef struct {
    Bit#(64) value;
} Word deriving (Bits, Eq, Ord, FShow);

function Word word(Bit#(64) value);
    return Word { value: value };
endfunction

typedef struct {
    Bit#(45) value;
} Addr deriving (Bits, Eq, Ord, FShow);

function Addr addr(Bit#(45) value);
    return Addr { value: value };
endfunction

interface Heap;
    interface BRAM1Port#(Addr, Word) even;
    interface BRAM1Port#(Addr, Word) odd;
endinterface

module mkHeap(Heap);
    BRAM_Configure cfg = defaultValue;
    cfg.memorySize = 32 * 1024;
    BRAM1Port#(Addr, Word) evenRam <- mkBRAM1Server(cfg);
    BRAM1Port#(Addr, Word) oddRam <- mkBRAM1Server(cfg);

    interface even = evenRam;
    interface odd = oddRam;
endmodule
