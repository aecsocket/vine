# Invoked by swim plugin vivado with ivga/build as the working directory.
set root [file normalize [file join [file dirname [info script]] ..]]
if {[file normalize [pwd]] ne [file join $root build]} {
    error "Run via swim plugin vivado (working directory must be ivga/build)"
}
# Never leave a previous successful bitstream after a failed new build.
file delete -force ivga.bit
read_verilog -sv [file join $root build spade.sv]

read_xdc [file join $root board arty.xdc]
synth_design -top top -part xc7a100tcsg324-1
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
