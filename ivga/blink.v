module blink (
  input logic clk, // 100MHz crystal on the board
  output logic led
);

localparam logic [26:0] COUNT_MAX = 1_000_000 - 1; // 100M cycles = 1 second

// 2^27 = 134,217,728 - just above 100,000,000 that we need
logic [26:0] count;

always_ff @(posedge clk) begin
  if (count == COUNT_MAX) begin
    count <= 0;
    led <= ~led;
  end else begin
    count <= count + 1;
  end
end

endmodule
