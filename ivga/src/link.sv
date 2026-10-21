// linking engine - some other piece of interaction logic tells us,
// "hey I want to link ports A and B together", and this logic actually does it.
module link
    import ports_pkg::*;
(
    input logic clk,
    input logic rst,
    heap_if.client heap,
    link_if.engine link
);

typedef enum logic [2:0] {
    IDLE,
    LINK,
    WAIT_A,
    FETCH_A,
    WAIT_B,
    FETCH_B
} state_t;

state_t state;
port_t a;
port_t b;

always_ff @(posedge clk) begin : cycle
    if (rst) begin
        state <= IDLE;
        link.done <= 1'b0;
        heap.write_enable <= 1'b0;
        heap.read_addr <= '0;
        disable cycle;
    end

    link.done <= 1'b0;
    heap.write_enable <= 1'b0;

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
            if (a.tag == TAG_ERASE && b.tag == TAG_ERASE) begin
                // if A and B are both erasers, then there's nothing to do;
                // we complete immediately
                link.done <= 1'b1;
                state <= IDLE;
            end else if (a.tag == TAG_WIRE && b.tag == TAG_WIRE) begin
                heap.read_addr <= a.payload;
                state <= WAIT_A;
            end else
                $fatal(1, "unimplemented");
        end
        WAIT_A: begin
            state <= FETCH_A;
        end
        FETCH_A: begin
            a <= heap.read_data;
            heap.read_addr <= b.payload;
            state <= WAIT_B;
        end
        WAIT_B: begin
            state <= FETCH_B;
        end
        FETCH_B: begin
            b <= heap.read_data;
            state <= LINK;
        end
        default: begin end
    endcase
end

endmodule
