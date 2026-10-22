module tb_alloc
    import ports_pkg::*;
();

localparam int TIMEOUT_CYCLES = 16;

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
        heap.bram.mem[index] = PORT_EMPTY;
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

task automatic wait_done(input string operation);
    for (int cycle = 0; cycle < TIMEOUT_CYCLES; cycle++) begin
        if (alloc_bus.done === 1'b1) begin
            if (alloc_bus.oom !== 1'b0)
                $fatal(1, "%s: unexpected out-of-memory", operation);
            return;
        end
        @(negedge clk);
    end
    $fatal(1, "%s: no completion within %0d cycles", operation, TIMEOUT_CYCLES);
endtask

task automatic expect_done_pulse();
    @(negedge clk);
    if (alloc_bus.done !== 1'b0)
        $fatal(1, "done must clear after completion without another request");
    if (heap_bus.write_enable !== 1'b0)
        $fatal(1, "RAM write enable must clear after completion");
endtask

task automatic expect_alloc(input addr_t expected);
    @(negedge clk);
    alloc_bus.alloc = 1'b1;
    @(negedge clk);
    alloc_bus.alloc = 1'b0;

    wait_done("allocation");
    if (alloc_bus.alloc_addr !== expected)
        $fatal(1, "expected allocation at %0d, got %0d", expected, alloc_bus.alloc_addr);
    expect_done_pulse();
endtask

task automatic free_block(input addr_t addr, input addr_t previous_head);
    @(negedge clk);
    alloc_bus.free_addr = addr;
    alloc_bus.free = 1'b1;
    @(negedge clk);
    alloc_bus.free = 1'b0;
    // The allocator must retain the accepted address, not use live inputs later.
    alloc_bus.free_addr = ADDR_NONE;

    wait_done("free");
    // Completion must mean the next-pointer has actually reached RAM.
    if (heap.bram.mem[addr] !== port_t'(previous_head))
        $fatal(1, "free %0d: expected stored next-pointer %0d, got %h",
            addr, previous_head, heap.bram.mem[addr]);
    expect_done_pulse();
endtask

task automatic test_free_zero();
    expect_alloc(addr_t'(0));
    free_block(addr_t'(0), ADDR_NONE);
    expect_alloc(addr_t'(0));
    expect_alloc(addr_t'(1));
endtask

task automatic test_free_nonzero();
    expect_alloc(addr_t'(0));
    expect_alloc(addr_t'(1));
    expect_alloc(addr_t'(2));
    free_block(addr_t'(1), ADDR_NONE);
    expect_alloc(addr_t'(1));
    expect_alloc(addr_t'(3));
endtask

task automatic test_free_lifo();
    expect_alloc(addr_t'(0));
    expect_alloc(addr_t'(1));
    expect_alloc(addr_t'(2));

    // Non-address order distinguishes a stack from bump or sorted allocation.
    free_block(addr_t'(0), ADDR_NONE);
    free_block(addr_t'(2), addr_t'(0));
    free_block(addr_t'(1), addr_t'(2));

    expect_alloc(addr_t'(1));
    expect_alloc(addr_t'(2));
    expect_alloc(addr_t'(0));
    expect_alloc(addr_t'(3));
endtask

task automatic test_repeated_reuse();
    expect_alloc(addr_t'(0));
    repeat (4) begin
        free_block(addr_t'(0), ADDR_NONE);
        expect_alloc(addr_t'(0));
    end
    expect_alloc(addr_t'(1));
endtask

task automatic test_reset_free_list();
    expect_alloc(addr_t'(0));
    expect_alloc(addr_t'(1));
    free_block(addr_t'(1), ADDR_NONE);
    reset();
    expect_alloc(addr_t'(0));
    expect_alloc(addr_t'(1));
endtask

initial begin
    reset();
    test_one_alloc();
    reset();
    test_many_allocs();
    reset();
    test_free_zero();
    reset();
    test_free_nonzero();
    reset();
    test_free_lifo();
    reset();
    test_repeated_reuse();
    reset();
    test_reset_free_list();

    $display("tests passed");
    $finish;
end

endmodule
