# Digilent Arty A7-100 Rev. D/E master XDC:
# https://github.com/Digilent/digilent-xdc/blob/master/Arty-A7-100-Master.xdc
set_property -dict {PACKAGE_PIN E3 IOSTANDARD LVCMOS33} [get_ports clk]
create_clock -name clk100 -period 10.000 [get_ports clk]
# Dedicated chipKIT RESET button: active low.
set_property -dict {PACKAGE_PIN C2 IOSTANDARD LVCMOS33} [get_ports reset]
set_property -dict {PACKAGE_PIN H5 IOSTANDARD LVCMOS33} [get_ports {led[0]}]
set_property -dict {PACKAGE_PIN J5 IOSTANDARD LVCMOS33} [get_ports {led[1]}]
set_property -dict {PACKAGE_PIN T9 IOSTANDARD LVCMOS33} [get_ports {led[2]}]
set_property -dict {PACKAGE_PIN T10 IOSTANDARD LVCMOS33} [get_ports {led[3]}]
# Configuration-bank supply settings for the Arty A7.
set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
