// abstraction over `bram` which uses `ports_pkg` types as parameters.
// short for "pair RAM".
module pram (
    input  logic             clk,
    input  logic             write_enable,
    input  ports_pkg::addr_t write_addr,
    input  ports_pkg::port_t write_data,
    input  ports_pkg::addr_t read_addr,
    output ports_pkg::port_t read_data
);

// so here, we make a block RAM with a data with of 64 bits,
// and the block RAM can only read one word (64 bits) at a time.
// but theoretically, a lot of our ops involve reading TWO words
// ESPECIALLY `Comb` interactions. for those ixns, could we instead
// have our memory read two words per clock cycle? :thinking:
// TODO: could we impl this, and get it to infer correctly on trhe FPGA?
bram #(
    .DATA_WIDTH($bits(ports_pkg::port_t)),
    .ADDR_WIDTH($bits(ports_pkg::addr_t))
) bram (
	.clk         (clk),
	.write_enable(write_enable),
	.write_addr  (write_addr),
	.write_data  (write_data),
	.read_addr   (read_addr),
	.read_data   (read_data)
);

endmodule
