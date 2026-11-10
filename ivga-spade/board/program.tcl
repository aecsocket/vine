# SRAM/JTAG programming only: no configuration-memory/flash operations.
set root [file normalize [file join [file dirname [info script]] ..]]
set bitstream [file join $root build ivga.bit]
if {![file isfile $bitstream]} {
    error "Missing $bitstream; run swim plugin vivado first"
}
open_hw_manager
connect_hw_server
# Inspect every attached target, not merely whichever Vivado chooses first.
set matches {}
foreach target [get_hw_targets] {
    current_hw_target $target
    open_hw_target
    foreach device [get_hw_devices] {
        if {[string equal -nocase [get_property PART $device] xc7a100t]} {
            lappend matches [list $target $device]
        }
    }
    close_hw_target
}
if {[llength $matches] != 1} {
    error "Expected exactly one xc7a100t device across all targets; found [llength $matches]. Nothing programmed."
}
lassign [lindex $matches 0] target device
current_hw_target $target
open_hw_target
# Re-query after reopening rather than retaining a potentially stale handle.
set devices [get_hw_devices -filter {PART == xc7a100t}]
if {[llength $devices] != 1} {
    error "Hardware changed during discovery; nothing programmed"
}
set device [lindex $devices 0]
current_hw_device $device
set_property PROGRAM.FILE $bitstream $device
program_hw_devices $device
refresh_hw_device $device
set done [get_property REGISTER.IR.BIT5_DONE $device]
if {$done ne "1"} {
    error "Programming did not assert DONE (REGISTER.IR.BIT5_DONE=$done)"
}
puts "Programmed $bitstream into volatile FPGA SRAM; DONE=1. Flash unchanged."
close_hw_target
disconnect_hw_server
close_hw_manager
