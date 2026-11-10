# Onboard 100 MHz oscillator.
set_property PACKAGE_PIN E3 [get_ports {CLK}]
set_property IOSTANDARD LVCMOS33 [get_ports {CLK}]
create_clock -name clk100 -period 10.000 [get_ports {CLK}]

# Active-low board reset, matching BSC's default active-low reset.
set_property PACKAGE_PIN C2 [get_ports {RST_N}]
set_property IOSTANDARD LVCMOS33 [get_ports {RST_N}]

# LED 0: enable after exposing an output named led in the generated Verilog.
set_property PACKAGE_PIN H5 [get_ports {led0}]
set_property IOSTANDARD LVCMOS33 [get_ports {led0}]

set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
