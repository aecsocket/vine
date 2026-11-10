package Top;

(* synthesize *)
module mkTop(Empty);
    Reg#(UInt#(32)) count <- mkReg(0);
    Reg#(Bool) led_on <- mkReg(False);

    rule tick;
        if (count == 1_000_000 - 1) begin
            count <= 0;
            led_on <= !led_on;
            $display("LED is now %b", pack(!led_on));
        end
        else begin
            count <= count + 1;
        end
    endrule
endmodule

endpackage
