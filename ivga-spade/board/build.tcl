# Invoked by swim plugin vivado with ivga/build as the working directory.
set root [file normalize [file join [file dirname [info script]] ..]]
if {[file normalize [pwd]] ne [file join $root build]} {
    error "Run via swim plugin vivado (working directory must be ivga/build)"
}
# Never leave a previous successful bitstream after a failed new build.
file delete -force ivga.bit
# Non-project flow: load the installed XPM sources before synthesis.
read_verilog -sv -library xpm [file join $::env(XILINX_VIVADO) data ip xpm xpm_memory hdl xpm_memory.sv]
proc systemverilog_sources {directory} {
    set sources [glob -nocomplain -types f -directory $directory *.sv]
    foreach child [glob -nocomplain -types d -directory $directory *] {
        lappend sources {*}[systemverilog_sources $child]
    }
    return [lsort $sources]
}
set shim_sources [systemverilog_sources [file join $root src]]
if {[llength $shim_sources] > 0} {
    read_verilog -sv $shim_sources
}
read_verilog -sv [file join $root build spade.sv]

read_xdc [file join $root board arty.xdc]
# Digilent's Arty A7 manual lists XC7A100TCSG324-1 for the 100T board.
# JTAG cannot distinguish its package/speed grade; verify the chip marking
# if using a board revision fitted with a different part.
synth_design -top top -part xc7a100tcsg324-1
# Exempt only the marked synchronizer preset pins, not every reset-input path.
set reset_cells [get_cells -hier -filter {RESET_SYNC == "TRUE"}]
set reset_pins [get_pins -of_objects $reset_cells -filter {REF_PIN_NAME == PRE}]
if {[llength $reset_cells] != 2 || [llength $reset_pins] != 2} {
    error "Expected two reset synchronizer preset pins; refusing to apply timing exception"
}
set_false_path -from [get_ports reset] -to $reset_pins
opt_design
place_design
route_design
report_utilization -file utilization.rpt
report_timing_summary -file timing.rpt
report_drc -file drc.rpt
set worst [get_timing_paths -delay_type max -max_paths 1]
if {[llength $worst] != 1 || [get_property SLACK $worst] < 0} {
    error "Missing or failing setup timing; see timing.rpt"
}
set worst_hold [get_timing_paths -delay_type min -max_paths 1]
if {[llength $worst_hold] != 1 || [get_property SLACK $worst_hold] < 0} {
    error "Missing or failing hold timing; see timing.rpt"
}
write_checkpoint -force ivga.dcp
write_bitstream -force ivga.bit
