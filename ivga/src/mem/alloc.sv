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
    WAIT_ALLOC,
    FETCH_ALLOC,
    WAIT_FREE
} state_t;

state_t state;

// ptr to the start of unallocated memory
addr_t empty;
// ptr to the head of the freelist
// the value at `heap[head]` is the address of the next free address
// that we can recycle.
addr_t head;
// when allocating, give this addr back as the user's allocation
addr_t allocd_head;

always_ff @(posedge clk) begin : cycle
    engine.alloc_addr <= addr_t'(0);
    engine.done <= 0;
    engine.oom <= 0;
    heap.write_enable <= 1'b0;

    if (rst) begin
        state <= IDLE;
        empty <= addr_t'(0);
        head <= ADDR_NONE;
        allocd_head <= addr_t'(0);
        disable cycle;
    end

    case (state)
        IDLE: begin
            if (engine.alloc) begin
                // client requested an allocation
                if (head == ADDR_NONE) begin
                    // TODO OOM handling

                    // our freelist is empty, we'll just give you a bit off
                    // the unalloc'd space
                    engine.alloc_addr <= empty;
                    engine.done <= 1;
                    empty <= empty + 1;
                end else begin
                    // we can reuse an allocation from before,
                    // but first we need to set `head` to the next addr in the freelist.
                    // for that, we need to read the value from memory.
                    heap.read_addr <= head;
                    state <= WAIT_ALLOC;
                    // also keep track of the current head, since that's what
                    // we'll give back to the client.
                    allocd_head <= head;
                end
            end else if (engine.free) begin
                // client requested to free `free_addr`;
                // overwrite what the client previously had there
                // with the addr of the current freelist head
                // (which will soon become the 2nd node in the list)
                //
                // let's imagine we just freed address 5.
                // current freelist:
                //   head = 15 -> mem[15] = 25 -> mem[25] = ADDR_NONE
                // new freelist:
                //   head = 5 -> mem[5] = 15 -> mem[15] = 25...
                heap.write_enable <= 1;
                heap.write_addr <= engine.free_addr;
                heap.write_data <= port_t'(head);

                head <= engine.free_addr;
                state <= WAIT_FREE;
            end
        end
        WAIT_ALLOC: begin
            state <= FETCH_ALLOC;
        end
        FETCH_ALLOC: begin
            head <= addr_t'(heap.read_data);
            engine.alloc_addr <= allocd_head;
            engine.done <= 1;
            state <= IDLE;
        end
        WAIT_FREE: begin
            engine.done <= 1;
            state <= IDLE;
        end
        default: begin end
    endcase
end

endmodule
