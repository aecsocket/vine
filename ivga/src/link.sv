// linking engine - some other piece of interaction logic tells us,
// "hey I want to link ports A and B together", and this logic actually does it.
module link
    import ports_pkg::*;
(
    input logic clk,
    input logic rst,
    heap_if.client heap,
    alloc_if.client alloc,
    link_if.engine link
);

typedef enum logic [2:0] {
    IDLE,
    LINK,
    WAIT_FREE,
    WAIT_FREE_AND_LINK,
    WAIT_WRITE,
    FOLLOW_WAIT,
    FOLLOW_FETCH
} state_t;

state_t state;
port_t a;
port_t b;

always_ff @(posedge clk) begin : cycle
    heap.write_enable <= 1'b0;
    heap.read_addr <= '0;
    alloc.free <= 1'b0;
    alloc.alloc <= 1'b0;
    link.done <= 1'b0;

    if (rst) begin
        a <= PORT_EMPTY;
        b <= PORT_EMPTY;
        state <= IDLE;
        disable cycle;
    end

    case (state)
        IDLE: begin : handle_idle
            if (!link.request)
                disable handle_idle;

            a <= link.a;
            b <= link.b;
            state <= LINK;
        end
        LINK: begin
            // by this point, `a` and `b` have been set to their correct values
            // for this link cycle

            if (
                a.tag == TAG_WIRE
                && b.tag == TAG_WIRE
                && a.payload == b.payload
            ) begin
                // A and B are wires which point to the same heap slot;
                // just free this slot and continue.
                alloc.free <= 1'b1;
                alloc.free_addr <= addr_t'(a.payload);
                state <= WAIT_FREE;
            end else if (a.tag == TAG_WIRE) begin
                // follow A all the way down first,
                // then link it to B
                heap.read_addr <= addr_t'(a.payload);
                state <= FOLLOW_WAIT;
            end else if (b.tag == TAG_WIRE) begin
                // follow B all the way down first,
                // then link it to A
                // treat it as the same as if A was a wire, but swapped
                heap.read_addr <= addr_t'(b.payload);
                a <= b;
                b <= a;
                state <= FOLLOW_WAIT;
            end else if (
                (a.tag == TAG_ERASE || a.tag == TAG_GRAFT)
                && (b.tag == TAG_ERASE || b.tag == TAG_GRAFT)
            ) begin
                // if A and B are both erasers, or both grafts,
                // then there's nothing to do; we complete immediately.
                //
                // but why do we do this for grafts as well? because a graft
                // indicates the root of a net which has *no* other connections
                // to any other outside net.
                // so if you have two grafts interacting with each other,
                // then computing them is useless, since there's no way they
                // could interact with anything else.
                link.done <= 1'b1;
                state <= IDLE;
            end else
                $fatal(1, "unimplemented");
        end

        WAIT_FREE: begin
            if (alloc.done) begin
                link.done <= 1'b1;
                state <= IDLE;
            end
        end

        WAIT_FREE_AND_LINK: begin
            if (alloc.done)
                state <= LINK;
        end

        WAIT_WRITE: begin
            link.done <= 1'b1;
            state <= IDLE;
        end

        FOLLOW_WAIT: begin
            // so what is following?
            // basically, you might have a port A, which is always a wire,
            // with a payload pointing at port N1. N1 might *itself* be a wire,
            // which points to port N2. N2 might also be a wire, and so on.
            //
            // we want to get to the end of this chain - find the first port
            // which *isn't* a wire, so we can actually do interaction on it.
            //
            // along the way, as we step through each wire in the heap, we can
            // also free that wire's memory, since it isn't being used anymore.
            // I kinda like to imagine it as: imagine you have a length of string
            // connected to 2 balls, or cans, or something.
            // Right now it's all jumbled up, with lots of knots.
            // Each iteration of FOLLOW undoes one of the knots, and at the end
            // you get back a single straight piece of string which connects
            // the 2 balls.
            // So each straightening does a memory free, and it's kinda also
            // "freeing up" the string so it can make a straight line.
            state <= FOLLOW_FETCH;
        end
        FOLLOW_FETCH: begin
            if (heap.read_data === PORT_EMPTY) begin
                // the wire points to an empty slot,
                // which will be populated later as we keep interacting.
                // so we need to write `b` into that slot,
                // for future interactions to work.
                heap.write_enable <= 1'b1;
                heap.write_addr <= addr_t'(a.payload);
                heap.write_data <= b;
                state <= WAIT_WRITE;
            end else begin
                // wire points to a populated slot;
                // we're consuming that slot, freeing it.
                // a.payload should equal heap.read_addr here
                alloc.free <= 1'b1;
                alloc.free_addr <= addr_t'(a.payload);

                // so we just restart linking using that as our A slot
                a <= heap.read_data;
                state <= WAIT_FREE_AND_LINK;
            end
        end
        default: begin end
    endcase
end

endmodule
