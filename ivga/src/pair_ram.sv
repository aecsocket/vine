// abstraction over `bram` which uses `ports_pkg` types as parameters.
module pair_ram (
    input  logic    clk,
    pairs_if.memory mem
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
	.write_enable(mem.write_enable),
	.write_addr  (mem.write_addr),
	.write_data  (mem.write_data),
	.read_addr   (mem.read_addr),
	.read_data   (mem.read_data)
);

endmodule
