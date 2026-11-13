import BRAM::*;

// Machine word.
typedef struct {
    Bit#(64) value;
} Word deriving (Bits, Eq, Ord, FShow);

function Word word(Bit#(64) value);
    return Word { value: value };
endfunction

// Address of a word in memory.
//
// Address 0 maps to the first word, address 1 maps to the second word, etc.
// This machine is *not* byte-addressed.
typedef struct {
    Bit#(45) value;
} Addr deriving (Bits, Eq, Ord, FShow);

function Addr addr(Bit#(45) value);
    return Addr { value: value };
endfunction

/// Address of a pair of words in memory.
typedef struct {
    Bit#(44) value;
} PairAddr deriving (Bits, Eq, Ord, FShow);

function PairAddr pairAddr(Bit#(44) value);
    return PairAddr { value: value };
endfunction

interface Heap;
    interface BRAM1Port#(PairAddr, Word) even;
    interface BRAM1Port#(PairAddr, Word) odd;
endinterface

module mkHeap(Heap);
    BRAM_Configure cfg = defaultValue;
    cfg.memorySize = 32 * 1024;
    BRAM1Port#(PairAddr, Word) evenRam <- mkBRAM1Server(cfg);
    BRAM1Port#(PairAddr, Word) oddRam <- mkBRAM1Server(cfg);

    interface even = evenRam;
    interface odd = oddRam;
endmodule
