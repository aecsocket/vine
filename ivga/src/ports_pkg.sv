package ports_pkg;
    // follows ivm/ports.rs conventions
    typedef enum logic [2:0] {
        TAG_INVALID = 3'd0,
        TAG_WIRE    = 3'd1,
        TAG_ERASE   = 3'd3,
        TAG_COMB    = 3'd5
    } tag_t;

    typedef logic [15:0] label_t;

    // a port's payload is actually 45 bits wide,
    // which would make this a `[44:0]`,
    // but that would mean we instantiate 2^45 bits of block RAM,
    // which is like terabytes of RAM. (IN THIS ECONOMY!?!?!)
    // so we keep the address size small to instantiate less.
    typedef logic [9:0] addr_t;

    typedef struct packed {
        label_t      label;
        logic [44:0] payload;
        tag_t        tag;
    } port_t;
endpackage
