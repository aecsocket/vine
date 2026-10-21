// linking engine - some other piece of interaction logic tells us,
// "hey I want to link ports A and B together", and this logic actually does it.
module link
    import ports_pkg::*;
(
    input  logic    clk,
    pairs_if.client pairs,
    link_if.engine  link
);

typedef enum logic [2:0] {
    IDLE,
    FETCH_AUX_A,
    WAIT_AUX_A,
    FETCH_AUX_B,
    WAIT_AUX_B,
    LINK
} state_t;

state_t state;
// when someone gives us a request to link A and B,
// we store A's and B's payloads (addresses) into here.
// so if on the next cycle they submit another request,
// it won't overwrite our current one.
addr_t a_addr;
port_t a_aux_port;
addr_t b_addr;
port_t b_aux_port;

always_ff @(posedge clk) begin
    link.done <= 1'b0;

    case (state)
        FETCH_AUX_A: begin
            // when we get a request to link nodes,
            // first thing we do is to fetch what A's and B's aux ports are.
            // this tells us *how* we link the two.
            a_addr <= link.a.payload;
            b_addr <= link.b.payload;
            pairs.read_addr <= link.a.payload;
            state <= FETCH_AUX_B;
        end
        FETCH_AUX_B: begin
            // now we've got A's aux port back. save it in our register.
            a_aux_port <= pairs.read_data;
            // and go fetch B.
            pairs.read_addr <= b_addr;
            state <= LINK;
        end
        LINK: begin
            // now we've got B's aux port.
            b_aux_port <= pairs.read_data;
            // we now know how to link A and B together.

            // if A and B are both erasers, then there's nothing to do;
            // we complete immediately
            if (a_aux_port.tag == TAG_ERASE && b_aux_port.tag == TAG_ERASE) begin
                link.done <= 1'b1;
                state <= DONE;
            end else
                $fatal(1, "unimplemented");
        end
        DONE: begin end
        default: begin end
    endcase
end

endmodule
