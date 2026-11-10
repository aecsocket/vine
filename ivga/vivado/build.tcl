# Run from ivga/: vivado -mode batch -source vivado/build.tcl \
#   -tclargs target /path/to/bsc/lib
if {$argc != 2} {
    error "Expected arguments: <build-directory> <bsc-library-directory>"
}

set project_dir [file dirname [file dirname [file normalize [info script]]]]
set target [file normalize [lindex $argv 0]]
set bsc_lib [file normalize [lindex $argv 1]]
set output [file join $target vivado]
set sources [glob -nocomplain [file join $target verilog *.v]]
if {[llength $sources] == 0} {
    error "No generated Verilog found; run just emit-verilog first"
}
if {![file isdirectory [file join $bsc_lib Verilog]]} {
    error "Missing BSC Verilog library: $bsc_lib/Verilog"
}
file mkdir $output
# Keep auxiliary Vivado files here even when invoked outside the just recipe.
cd $output

# Match the part used by ivga-amaranth's Arty A7-100T build.
create_project -in_memory -part xc7a100ticsg324-1L
add_files -norecurse $sources

# BSC primitives are synthesis sources; main.v is a simulation-only harness.
foreach source [glob [file join $bsc_lib Verilog *.v]] {
    if {[file tail $source] ne "main.v"} {
        add_files -norecurse $source
    }
}
set_property include_dirs [list [file join $bsc_lib Verilog]] [current_fileset]
read_xdc [file join $project_dir src Top.xdc]

synth_design -top mkTop
opt_design
place_design
phys_opt_design
route_design

report_timing_summary -file [file join $output timing.rpt]
report_utilization -file [file join $output utilization.rpt]
report_drc -file [file join $output drc.rpt]
write_checkpoint -force [file join $output routed.dcp]

# Do not silently generate a bitstream for a design that fails setup/hold timing.
foreach delay_type {max min} {
    set paths [get_timing_paths -quiet -delay_type $delay_type -max_paths 1]
    if {[llength $paths] > 0 && [get_property SLACK [lindex $paths 0]] < 0} {
        error "Timing failed ($delay_type); see $output/timing.rpt"
    }
}
write_bitstream -force [file join $output top.bit]
