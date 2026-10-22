from amaranth import *
from amaranth.lib import wiring
from amaranth.lib.wiring import In, Out


class UpCounter(wiring.Component):
    enable: In(1)
    overflow: Out(1)

    def __init__(self, limit: int):
        self.limit = limit
        self.count = Signal(16)
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        m.d.comb += self.overflow.eq(self.count == self.limit)

        with m.If(self.enable):
            with m.If(self.overflow):
                m.d.sync += self.count.eq(0)
            with m.Else():
                m.d.sync += self.count.eq(self.count + 1)

        return m
