`timescale 1ns/1ps

module xpm_sdpram #(
    parameter integer DATA_SZ,
    parameter integer ADDR_SZ,
    parameter integer DEPTH
) (
    input wire clk,
    input wire read_enable,
    input wire [ADDR_SZ-1:0] read_addr,
    input wire [ADDR_SZ-1:0] write_addr,
    input wire write_enable,
    input wire [DATA_SZ-1:0] write_data,
    output wire [DATA_SZ-1:0] read_data
);
    initial begin
        if (DATA_SZ < 1 || DEPTH < 1 || ADDR_SZ < 1)
            $fatal(1, "xpm_sdpram: DATA_SZ, DEPTH and ADDR_SZ must be positive");
        if (ADDR_SZ < $clog2(DEPTH))
            $fatal(1, "xpm_sdpram: ADDR_SZ cannot address DEPTH words");
    end

    xpm_memory_sdpram #(
        .MEMORY_SIZE(DATA_SZ * DEPTH),
        .MEMORY_PRIMITIVE("block"),
        .CLOCKING_MODE("common_clock"),
        .WRITE_DATA_WIDTH_A(DATA_SZ),
        .BYTE_WRITE_WIDTH_A(DATA_SZ),
        .READ_DATA_WIDTH_B(DATA_SZ),
        .ADDR_WIDTH_A(ADDR_SZ),
        .ADDR_WIDTH_B(ADDR_SZ),
        .WRITE_MODE_B("read_first"),
        .READ_LATENCY_B(1),
        .USE_MEM_INIT(1),
        .MEMORY_INIT_FILE("none"),
        .MEMORY_INIT_PARAM("0"),
        .SIM_ASSERT_CHK(1)
    ) memory (
        .clka(clk), .clkb(clk),
        .regceb(1'b1),
        // write port
        .ena(1'b1), .wea(write_enable), .addra(write_addr), .dina(write_data),
        // read port
        .enb(read_enable), .addrb(read_addr), .doutb(read_data),
        .rstb(1'b0), .sleep(1'b0),
        .injectsbiterra(1'b0), .injectdbiterra(1'b0),
        .sbiterrb(), .dbiterrb()
    );
endmodule
