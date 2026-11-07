`timescale 1ns/1ps

module xpm_sdpram #(
    parameter integer WIDTH = 64,
    parameter integer DEPTH = 65536,
    parameter integer INDEX = (DEPTH > 1 ? $clog2(DEPTH) : 1)
) (
    input wire clk,
    input wire [INDEX-1:0] read_addr,
    input wire [INDEX-1:0] write_addr,
    input wire write_enable,
    input wire [WIDTH-1:0] write_data,
    output wire [WIDTH-1:0] read_data
);
    initial begin
        if (WIDTH < 1 || DEPTH < 1 || INDEX < 1)
            $fatal(1, "xpm_sdpram: WIDTH, DEPTH and INDEX must be positive");
        if (INDEX < $clog2(DEPTH))
            $fatal(1, "xpm_sdpram: INDEX cannot address DEPTH words");
    end

    xpm_memory_sdpram #(
        .MEMORY_SIZE(WIDTH * DEPTH),
        .MEMORY_PRIMITIVE("block"),
        .CLOCKING_MODE("common_clock"),
        .WRITE_DATA_WIDTH_A(WIDTH),
        .BYTE_WRITE_WIDTH_A(WIDTH),
        .READ_DATA_WIDTH_B(WIDTH),
        .ADDR_WIDTH_A(INDEX),
        .ADDR_WIDTH_B(INDEX),
        .WRITE_MODE_B("read_first"),
        .READ_LATENCY_B(1),
        .USE_MEM_INIT(1),
        .MEMORY_INIT_FILE("none"),
        .MEMORY_INIT_PARAM("0"),
        .SIM_ASSERT_CHK(1)
    ) memory (
        .clka(clk), .clkb(clk),
        .ena(1'b1), .enb(1'b1), .regceb(1'b1),
        .wea(write_enable), .addra(write_addr), .dina(write_data),
        .addrb(read_addr), .doutb(read_data),
        .rstb(1'b0), .sleep(1'b0),
        .injectsbiterra(1'b0), .injectdbiterra(1'b0),
        .sbiterrb(), .dbiterrb()
    );
endmodule
