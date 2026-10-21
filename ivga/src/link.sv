// linking engine - some other piece of interaction logic tells us,
// "hey I want to link ports A and B together", and this logic actually does it.
module link
    import ports_pkg::*;
(
    // global
    input  logic  clk,
    // pair RAM
    output logic  pairs_write_enable,
    output addr_t pairs_write_addr,
    output port_t pairs_write_data,
    output addr_t pairs_read_addr,
    input  port_t pairs_read_data,
    // module-specific stuff
    input  logic  request,
    input  port_t a,
    input  port_t b,
    output logic  done
);

always_ff @(posedge clk) begin
    done <= 1'b0;

    if (request) begin
        // if A and B are both erasers, then there's nothing to do;
        // we complete immediately
        if (a.tag == TAG_ERASE && b.tag == TAG_ERASE)
            done <= 1'b1;
        else
            $fatal(1, "unimplemented");
    end
end


endmodule
