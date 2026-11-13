import Xpm::*;

typedef struct {
    Bit#(64) value;
} Word deriving (Bits, Eq, FShow);

typedef struct {
    Bit#(45) value;
} Addr deriving (Bits, Eq, FShow);

module mkHeap(Empty);
    XpmSdpram#(Addr, Word) mem <- mkXpmSdpram(32 * 1024);


endmodule
