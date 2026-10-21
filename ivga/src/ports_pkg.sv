package ports_pkg;
    // follows ivm/ports.rs conventions
    typedef enum logic [2:0] {
        TAG_INVALID = 3'd0,
        TAG_COMB    = 3'd5
    } tag_t;

    // a port's payload is actually 45 bits wide,
    // which would make this a `[44:0]`,
    // but that would mean we instantiate 2^45 bits of block RAM,
    // which is like terabytes of RAM. (IN THIS ECONOMY!?!?!)
    // so we keep the address size small to instantiate less.
    typedef logic [9:0] addr_t;

    typedef struct packed {
        logic [15:0] label;
        logic [44:0] payload;
        tag_t        tag;
    } port_t;
endpackage
