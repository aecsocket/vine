import Xpm::*;

typedef struct {
    Bit#(64) value;
} Word deriving (Bits, Eq, FShow);

typedef struct {
    Bit#(45) value;
} Addr deriving (Bits, Eq, FShow);

module mkHeap(Empty);
    XpmSdpram(Addr, Word) even <- mkXpmSdpram(256 * 1024);
    XpmSdpram(Addr, Word) odd <- mkXpmSdpram(256 * 1024);

endmodule
