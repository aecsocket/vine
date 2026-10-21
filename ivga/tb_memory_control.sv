module tb_memory_control;
  logic clk;
  logic [31:0] count;

  memory_control dut (.clk(clk), .count(count));

  always #1 clk = ~clk;

  initial begin
    $display("count is %d", count);
    repeat (100) @(posedge clk);
    $display("count is %d", count);
    repeat (100) @(posedge clk);
    $display("count is %d", count);
    repeat (100) @(posedge clk);
    $display("count is %d", count);
    repeat (100) @(posedge clk);
    $display("count is %d", count);
    $finish;
  end

endmodule
