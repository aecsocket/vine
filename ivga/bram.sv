// https://docs.amd.com/r/en-US/ug901-vivado-synthesis/Simple-Dual-Port-Block-RAM-with-Single-Clock-Verilog
module bram #(
  parameter int DATA_WIDTH = 32,
  parameter int ADDR_WIDTH = 10
) (
  input  logic                  clk,
  input  logic                  write_enable,
  input  logic [ADDR_WIDTH-1:0] write_addr,
  input  logic [DATA_WIDTH-1:0] write_data,
  input  logic [ADDR_WIDTH-1:0] read_addr,
  output logic [DATA_WIDTH-1:0] read_data
);

localparam int DEPTH = 2 ** ADDR_WIDTH;

logic [DATA_WIDTH-1:0] mem [DEPTH];

always_ff @(posedge clk) begin
  if (write_enable) mem[write_addr] <= write_data;
  read_data <= mem[read_addr];
end

endmodule
