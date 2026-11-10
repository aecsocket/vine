# JTAG programming is volatile: it does not write the board's configuration flash.
# Optional second argument selects a device when several are connected.
if {$argc < 1 || $argc > 2} {
    error "Expected arguments: <bitstream> ?device-name?"
}
set bitstream [file normalize [lindex $argv 0]]
if {![file isfile $bitstream]} {
    error "Bitstream not found: $bitstream; run just build first"
}

# Keep auxiliary hardware-manager files alongside the bitstream.
cd [file dirname $bitstream]

open_hw_manager
connect_hw_server
open_hw_target

set devices [get_hw_devices]
if {$argc == 2 && [lindex $argv 1] ne ""} {
    set requested [lindex $argv 1]
    set matches {}
    foreach device $devices {
        if {$device eq $requested} {
            lappend matches $device
        }
    }
    set devices $matches
}
if {[llength $devices] != 1} {
    error "Expected exactly one selected device. Available devices: [get_hw_devices]. Pass its name to just program."
}
set device [lindex $devices 0]
current_hw_device $device
refresh_hw_device $device
if {[get_property PART $device] ne "xc7a100t"} {
    error "Expected an xc7a100t FPGA, found [get_property PART $device]"
}
set_property PROGRAM.FILE $bitstream $device
program_hw_devices $device
close_hw_manager
