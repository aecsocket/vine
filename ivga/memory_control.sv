module memory_control(
  input logic clk,
  output [31:0] count
);

logic [31:0] mem_read_data;
logic [31:0] mem_write_data;

logic [31:0] local_count;

memory #(
  .DATA_WIDTH(32),
  .ADDR_WIDTH(10)
) my_mem (
  .clk(clk),
  .write_enable(1'b1),
  .write_addr('0),
  .write_data(mem_write_data),
  .read_addr('0),
  .read_data(mem_read_data)
);

always_ff @(posedge clk) begin
  mem_write_data <= mem_read_data + 1;
  local_count <= mem_write_data;
end

assign count = local_count;

endmodule
