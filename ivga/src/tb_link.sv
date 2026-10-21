module tb_link
    import ports_pkg::*;
();

localparam label_t DUMMY_LABEL = 0;

logic clk = 0;
logic rst = 0;

pair_ram pairs (
	.clk         (clk),
	.write_enable(write_enable),
	.write_addr  (write_addr),
	.write_data  (write_data),
	.read_addr   (read_addr),
	.read_data   (read_data)
);

link dut (
   	.clk(clk),
   	.a  ('{
        label: DUMMY_LABEL,
        payload: 45'd0,
        tag: TAG_ERASE
    }),
   	.b  ('{
        label: DUMMY_LABEL,
        payload: 45'd0,
        tag: TAG_ERASE
    })
);

endmodule
