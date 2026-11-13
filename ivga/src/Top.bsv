package Top;

import BRAM::*;
import DefaultValue::*;
import GetPut::*;

interface Leds;
    (* always_ready, result = "led0" *)
    method Bool led0;
endinterface

(* synthesize *)
module mkTop(Leds);
    Reg#(Bit#(4)) count <- mkReg(0);
    Reg#(Bool) led_on <- mkReg(False);

    BRAM_Configure cfg = defaultValue;
    cfg.memorySize = 16;
    BRAM1Port#(Bit#(4), Bit#(64)) ram <- mkBRAM1Server(cfg);

    Reg#(Bit#(1)) phase <- mkReg(0);

    rule phase0 (phase == 0);
        ram.portA.request.put(BRAMRequest {
            write: False,
            responseOnWrite: False,
            address: count,
            datain: 0
        });
        phase <= 1;
    endrule

    rule phase1 (phase == 1);
        let value <- ram.portA.response.get;
        $display("mem[%h] = %h", count, value);

        ram.portA.request.put(BRAMRequest {
            write: True,
            responseOnWrite: False,
            address: count,
            datain: value + 1
        });
        count <= count + 1;
        phase <= 0;
    endrule

    method Bool led0;
        return led_on;
    endmethod
endmodule

endpackage
