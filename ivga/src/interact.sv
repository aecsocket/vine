// performs interaction between two ports.
//
// remember how interaction nets work - you find any two agents/nodes where
// their principal ports are linked to each other, and that means the two nodes
// can interact. and theres only these kinds of interactions:
// - two eraser nodes annihilate each other
// - two nodes of the same *label* annihilate each other, and link the wires
//   together. so node A's port 1 is wired to B's port 1; same for port 2.
// - an eraser and a node will duplicate the node, and wire the erasers up to
//   each port in the node.
// - two nodes of different *labels* will commute - turn it into 4 nodes (im
//   still a little unclear on this exactly)
module interact
    import ports_pkg::*;
(
    input logic clk,
    heap_if.client heap,
    link_if.client link,
    interact_if.engine interact
);

typedef enum logic [2:0] {
    IDLE,
    ANNIHILATE_WAIT_LINK_A,
    ANNIHILATE_WAIT_LINK_B
} state_t;

state_t state;
port_t annihilate_a;
port_t annihilate_b;

// if A and B are two principal ports of nodes NA and NB which combine together,
// AND A and B are ports of the same label of node,
// then they will annihilate each other.
// this means that NA's first port is linked to NB's first port,
// and same with the 2nd port.
logic annihilate;
assign annihilate =
    interact.a.tag == ports_pkg::TAG_COMB
    && interact.b.tag == ports_pkg::TAG_COMB
    && interact.a.label == interact.b.label;

always_ff @(posedge clk) begin
    link.request <= 1'b0;

    case (state)
        // the `: handle_idle` stuff is essentially like an early return guard
        IDLE: begin : handle_idle
            if (!interact.request)
                disable handle_idle;

            if (annihilate) begin
                annihilate_a <= interact.a;
                annihilate_b <= interact.b;

                // to annihilate NA and NB, we say:
                // - make up two fresh wires, which connect to:
                //   - one connects to NA's first aux port (a.payload)
                //   - the other connects to NB's first aux port (b.payload)
                //   - (note: the wires arent stored in memory, just in our hardware registers)
                // - tell the link engine to link these two wires together
                //   - so basically, "link NA's aux port 1 and NB's aux port 1"
                link.a <= '{
                    label: '0,
                    payload: interact.a.payload,
                    tag: TAG_WIRE
                };
                link.b <= '{
                    label: '0,
                    payload: interact.b.payload,
                    tag: TAG_WIRE
                };
                // submit the request to the link engine,
                // and wait for it to finish
                link.request <= 1'b1;
                state <= ANNIHILATE_WAIT_LINK_A;
            end
        end
        ANNIHILATE_WAIT_LINK_A: begin
            if (link.done) begin
                // NA aux port 1 and NB aux port 1 have been linked;
                // now do NA/NB aux port 2
                link.a <= '{
                    label: '0,
                    payload: annihilate_a.payload + addr_t'(1),
                    tag: TAG_WIRE
                };
                link.b <= '{
                    label: '0,
                    payload: annihilate_b.payload + addr_t'(1),
                    tag: TAG_WIRE
                };
                link.request <= 1'b1;
                state <= ANNIHILATE_WAIT_LINK_B;
            end
        end
        ANNIHILATE_WAIT_LINK_B: begin
            if (link.done) begin
                // NA/NB aux port 2 are linked;
                // annihilation is complete
                state <= IDLE;
            end
        end
        default: begin end
    endcase
end

endmodule
