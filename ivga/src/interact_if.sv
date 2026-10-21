interface interact_if
    import ports_pkg::*;
();

logic request;
port_t a;
port_t b;
logic done;

modport engine (
    input request, a, b,
    output done
);

modport client (
    output request, a, b,
    input done
);

endinterface
