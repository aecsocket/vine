interface heap_if
    import ports_pkg::*;
();

logic write_enable;
addr_t write_addr;
port_t write_data;
addr_t read_addr;
port_t read_data;

modport memory (
    input  write_enable, write_addr, write_data, read_addr,
    output read_data
);

modport client (
    output write_enable, write_addr, write_data, read_addr,
    input  read_data
);

endinterface
