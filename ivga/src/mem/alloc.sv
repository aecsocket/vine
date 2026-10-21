// bump allocator with a free list, lets you allocate memory on the heap
// for nodes and ports.
//
// this is basically the same as the IVM's allocator, but implemented in hardware.
// thankfully it's a pretty simple allocator :)
module alloc
    import ports_pkg::*;
(
    input logic clk,
    input logic rst,
    heap_if.client heap,
    alloc_if.engine engine
);

typedef enum logic [2:0] {
    IDLE,
    WAIT_HEAD
} state_t;

state_t state;

// ptr to the start of unallocated memory
addr_t empty;
// ptr to the head of the freelist
// the value at `heap[head]` is the address of the next free address
// that we can recycle.
addr_t head;
addr_t old_head;

always_ff @(posedge clk) begin : cycle
    if (rst) begin
        engine.alloc_addr <= 0;
        engine.alloc_done <= 0;
        engine.oom <= 0;
        state <= IDLE;
        empty <= addr_t'(0);
        head <= ADDR_NONE;
        old_head <= addr_t'(0);
        disable cycle;
    end

    engine.alloc_addr <= addr_t'(0);
    engine.alloc_done <= 0;
    engine.oom <= 0;

    case (state)
        IDLE: begin
            if (engine.alloc) begin
                // client requested an allocation
                if (head == ADDR_NONE) begin
                    // TODO OOM handling

                    // our freelist is empty, we'll just give you a bit off
                    // the unalloc'd space
                    engine.alloc_addr <= empty;
                    engine.alloc_done <= 1;
                    empty <= empty + 1;
                end else begin
                    // we can reuse an allocation from before,
                    // but first we need to set `head` to the next addr in the freelist.
                    // for that, we need to read the value from memory.
                    heap.read_addr <= head;
                    state <= WAIT_HEAD;
                    // also keep track of the current head, since that's what
                    // we'll give back to the client.
                    old_head <= head;
                end
            end
        end
        WAIT_HEAD: begin
            head <= heap.read_data;
            engine.alloc_addr <= old_head;
            engine.alloc_done <= 1;
            state <= IDLE;
        end
        default: begin end
    endcase
end

endmodule
