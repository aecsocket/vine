module tb_heap
    import ports_pkg::*;
();

localparam port_t INVALID = '{label: '0, payload: '0, tag: TAG_INVALID};
localparam port_t ERASER = '{label: '0, payload: '0, tag: TAG_ERASE};
localparam port_t PORT_1 = '{label: '0, payload: 1, tag: TAG_ERASE};
localparam port_t PORT_2 = '{label: '0, payload: 2, tag: TAG_ERASE};

logic clk;
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
        heap.bram.mem[index] = INVALID;
end

// tests
task automatic test_read_uninit();
    heap_bus.read_addr = 0;
    @(posedge clk);
    if (heap_bus.read_data != INVALID)
        $fatal(1, "expected to read invalid port");
endtask

task automatic test_write_read_zero();
    heap_bus.read_addr = addr_t'(1337);
    heap_bus.write_addr = 0;
    heap_bus.write_enable = 1;
    heap_bus.write_data = ERASER;
    @(posedge clk);

    heap_bus.read_addr = addr_t'(0);
    if (heap_bus.read_data == ERASER)
        $fatal(1, "expected to read non-eraser port first");
    @(posedge clk);
    if (heap_bus.read_data != ERASER)
        $fatal(1, "expected to read eraser port");
endtask

task automatic test_write_read_addrs();
    heap_bus.read_addr = addr_t'(1337);
    heap_bus.write_addr = addr_t'(123);
    heap_bus.write_enable = 1;
    heap_bus.write_data = PORT_1;
    @(posedge clk);

    heap_bus.write_addr = addr_t'(456);
    heap_bus.write_enable = 1;
    heap_bus.write_data = PORT_2;
    @(posedge clk);

    heap_bus.read_addr = addr_t'(123);
    @(posedge clk);
    if (heap_bus.read_data != PORT_1)
        $fatal(1, "expected to read port 1");

    heap_bus.read_addr = addr_t'(456);
    @(posedge clk);
    if (heap_bus.read_data != PORT_2)
        $fatal(1, "expected to read port 2");
endtask

initial begin
    test_read_uninit();
    test_write_read_zero();
    $display("tests passed");
    $finish;
end

endmodule
