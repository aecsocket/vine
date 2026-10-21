interface alloc_if
    import ports_pkg::*;
();

logic alloc;
logic free;
addr_t free_addr;
addr_t alloc_addr;
logic done;
logic oom;

modport engine (
    input alloc, free, free_addr,
    output alloc_addr, done, oom
);

modport client (
    output alloc, free, free_addr,
    input alloc_addr, done, oom
);

endinterface
