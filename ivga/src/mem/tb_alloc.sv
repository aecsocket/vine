module tb_alloc
    import ports_pkg::*;
();

logic clk;
logic rst = 1;
initial begin
    clk = 1'b0;
    forever #1 clk = ~clk;
end

// set up heap memory
heap_if heap_bus();
heap heap (
	.clk(clk),
	.mem(heap_bus)
);
initial begin
    foreach (heap.bram.mem[index])
        heap.bram.mem[index] = PORT_INVALID;
end

// set up allocator
alloc_if alloc_bus();
alloc alloc (
	.clk   (clk),
	.rst   (rst),
	.heap  (heap_bus),
	.engine(alloc_bus)
);

task automatic reset();
    rst = 1;
    alloc_bus.alloc = 0;
    alloc_bus.free = 0;
    alloc_bus.free_addr = addr_t'(0);
    repeat (2) @(negedge clk);
    rst = 1'b0;
endtask

// tests
task automatic test_one_alloc();
    if (alloc_bus.done !== 0)
        $fatal(1, "expected done to be 0 before alloc");
    @(negedge clk);
    alloc_bus.alloc = 1;

    @(negedge clk);
    alloc_bus.alloc = 0;

    if (alloc_bus.done !== 1)
        $fatal(1, "expected bump allocation to finish in 1 cycle");
    if (alloc_bus.alloc_addr !== addr_t'(0))
        $fatal(1, "expected bump allocation to allocate address 0");
endtask

task automatic test_many_allocs();
    if (alloc_bus.done !== 0)
        $fatal(1, "expected done to be 0 before alloc");
    @(negedge clk);
    alloc_bus.alloc = 1;

    @(negedge clk);
    alloc_bus.alloc = 0;

    if (alloc_bus.done !== 1)
        $fatal(1, "expected first bump allocation to finish in 1 cycle");
    if (alloc_bus.alloc_addr != addr_t'(0))
        $fatal(1, "expected first bump allocation to allocate address 0");

    @(negedge clk);
    alloc_bus.alloc = 1;

    @(negedge clk);
    alloc_bus.alloc = 0;

    if (alloc_bus.done !== 1)
        $fatal(1, "expected second bump allocation to finish in 1 cycle");
    if (alloc_bus.alloc_addr === addr_t'(0))
        $fatal(1, "expected second bump allocation to allocate above address 0");
endtask

initial begin
    reset();
    test_one_alloc();
    reset();
    test_many_allocs();

    $display("tests passed");
    $finish;
end

endmodule
