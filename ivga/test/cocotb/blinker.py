# top=main
import cocotb
from cocotb.triggers import Timer


async def settle():
    await Timer(5, units="ns")


async def tick(dut, reset=False):
    dut.clk_i.value = 0
    dut.rst_i.value = int(reset)
    await settle()
    dut.clk_i.value = 1
    await settle()


@cocotb.test()
async def synchronous_reset_and_count(dut):
    await tick(dut, reset=True)
    assert int(dut.count.value) == 0
    assert int(dut.output__.value) == 0
    for expected in range(1, 33):
        await tick(dut)
        assert int(dut.count.value) == expected
    dut.clk_i.value = 0
    dut.rst_i.value = 1
    await settle()
    assert int(dut.count.value) == 32, "reset must wait for a rising edge"
    await tick(dut, reset=True)
    assert int(dut.count.value) == 0
    await tick(dut)
    assert int(dut.count.value) == 1


@cocotb.test()
async def led_threshold_and_wrap(dut):
    await tick(dut, reset=True)
    # White-box boundary seeding avoids simulating 100 million Python clock cycles.
    for seed, expected_counts in (
        (49_999_999, (50_000_000, 50_000_001, 50_000_002)),
        (99_999_999, (100_000_000, 0, 1)),
    ):
        dut.clk_i.value = 0
        await settle()
        dut.count.value = seed
        await settle()
        assert int(dut.output__.value) == int(seed > 50_000_000)
        for expected in expected_counts:
            await tick(dut)
            assert int(dut.count.value) == expected
            assert int(dut.output__.value) == int(expected > 50_000_000)
    dut.clk_i.value = 0
    await settle()
    dut.count.value = 75_000_000
    await settle()
    assert int(dut.output__.value) == 1
    await tick(dut, reset=True)
    assert int(dut.count.value) == 0
    assert int(dut.output__.value) == 0
