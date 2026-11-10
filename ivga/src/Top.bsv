package Top;

(* synthesize *)
module mkTop(Empty);
    rule top;
        $display("Hello, world!");
        $finish(0);
    endrule
endmodule

endpackage
