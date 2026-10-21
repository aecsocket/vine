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
module interact (
    input  logic clk,
    input  logic rst,
    input  logic mem_read_data,
    input  ports_pkg::port_t a,
    input  ports_pkg::port_t b,
    output logic mem_write_enable,
    output logic mem_write_addr,
    output logic mem_write_data,
);

// if A and B are two principal ports which combine together,
// AND A and B are ports of the same label of node,
// then they will annihilate each other.
// this means that A's first port is linked to B's first port, and same with the
// 2nd port.
assign annihilate =
    a.tag == ports_pkg::TAG_COMB
    && b.tag == ports_pkg::TAG_COMB
    && a.label == b.label;
// if this happens, then `(a|b).payload` are memory indices, which "point to
// nodes in memory" according to the IVM. in our hardware, this means it points
// to the *first of two aux ports* on that node. and that port is, itself, a
// `port_t`.
//
// given this, we want to:
// - fetch the two aux ports of both A and B from memory (we use a state machine for
//   this i guess?)
//   - this gives us `a_aux_1`, `a_aux_2`, `b_aux_1`, `b_aux_2`
// - link a_aux_1 to b_aux_1, and a_aux_2 to b_aux_2
//   - this is done by writing back into memory

endmodule
