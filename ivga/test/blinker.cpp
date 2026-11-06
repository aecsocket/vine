#include "Vtop.h"
#include "verilated.h"
#include <cstdint>
#include <cstdlib>
#include <iostream>

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    Vtop dut;
    uint32_t count = 0;
    unsigned sync = 3;
    auto tick = [&](bool reset) {
        dut.clk = 0;
        dut.reset = reset;
        dut.eval();
        if (reset) sync = 3;
        count = (sync & 2) ? 0 : ((count + 1) & 0x0fffffff);
        sync = reset ? 3 : ((sync << 1) & 3);
        dut.clk = 1;
        dut.eval();
        if (dut.led != (count >> 24)) {
            std::cerr << "LED mismatch at count " << count
                      << ": got " << unsigned(dut.led) << '\n';
            std::exit(1);
        }
    };
    // Startup without a button press, all LED transitions, and full wraparound.
    for (uint32_t i = 0; i < (1u << 28) + 4; ++i) tick(false);
    // Reach a nonzero LED state, then verify button reset and synchronized release.
    for (uint32_t i = 0; i < (1u << 24); ++i) tick(false);
    tick(true);
    tick(true);
    for (unsigned i = 0; i < 8; ++i) tick(false);
    // A button pulse entirely between clock edges must still reset both stages.
    // The counter itself remains synchronous, so LEDs change only on a clock.
    for (uint32_t i = 0; i < (1u << 24); ++i) tick(false);
    dut.clk = 0;
    dut.eval();
    const auto before_reset = dut.led;
    dut.reset = 1;
    dut.eval();
    dut.reset = 0;
    dut.eval();
    if (dut.led != before_reset) {
        std::cerr << "Counter reset asynchronously instead of synchronously\n";
        return 1;
    }
    sync = 3;
    // Cross an LED boundary to expose even a one-cycle release error.
    for (uint32_t i = 0; i < (1u << 24) + 4; ++i) tick(false);
    dut.final();
    std::cout << "PASS: startup, all LED transitions, 28-bit wrap, button reset/release, inter-edge reset pulse\n";
}
