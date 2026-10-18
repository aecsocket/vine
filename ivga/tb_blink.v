module tb_blink;
  logic clk;
  logic led;

  blink dut (.clk(clk), .led(led));

  always #5 clk = ~clk;

  initial begin
    // repeat (200_000_000) @(posedge clk);
    repeat (1_000_000) @(posedge clk);
    $display("LED is %b", led);
    repeat (1_000_000) @(posedge clk);
    $display("LED is %b", led);
    repeat (1_000_000) @(posedge clk);
    $display("LED is %b", led);
    $finish;
  end

endmodule
