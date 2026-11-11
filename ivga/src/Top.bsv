package Top;

interface Leds;
    (* always_ready, result = "led0" *)
    method Bool led0;
endinterface

(* synthesize *)
module mkTop(Leds);
    Reg#(UInt#(32)) count <- mkReg(0);
    Reg#(Bool) led_on <- mkReg(False);

    rule tick;
        if (count == 50_000_000 - 1) begin
            count <= 0;
            led_on <= !led_on;
            $display("LED is now %b", pack(!led_on));
        end
        else begin
            count <= count + 1;
        end
    endrule

    method Bool led0;
        return led_on;
    endmethod
endmodule

endpackage
