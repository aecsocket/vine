package Xpm;

interface XpmSdpram#(type addr, type data);
    method Action read(addr address);
    method data readData;

    method Action write(addr address, data value);
endinterface

import "BVI" xpm_sdpram =
module mkXpmSdpram#(Integer depth)(XpmSdpram#(addr, data))
    provisos (
        Bits#(addr, addrWidth),
        Bits#(data, dataWidth)
    );

    parameter DATA_SZ = valueOf(dataWidth);
    parameter ADDR_SZ = valueOf(addrWidth);
    parameter DEPTH = depth;

    default_clock clk(clk);
    no_reset;

    method read(read_addr)
        enable(read_enable);
    method read_data readData;

    method write(write_addr, write_data)
        enable(write_enable);

    schedule read C read;
    schedule write C write;
    schedule read CF write;
    schedule readData CF (read, write, readData);
endmodule

endpackage
