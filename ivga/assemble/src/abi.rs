//! Hardware image ABI. Keep these layouts and registry IDs in sync with
//! ivga/__init__.py, mem/page_alloc.py, runtime/grafter.py, and extrinsic/.

pub const WORD_WIDTH: u32 = 64;
pub const ADDR_WIDTH: u32 = 10;
pub const PAGE_SHIFT: u32 = 7;
pub const PAGE_SIZE: usize = 1 << PAGE_SHIFT;
pub const CAPACITY: usize = 1 << ADDR_WIDTH;
pub const MAX_REGISTERS: usize = 1 << ADDR_WIDTH;
pub const SWAP_BIT: u16 = 1 << 15;

pub const TAG_GRAFT: u8 = 2;
pub const TAG_ERASE: u8 = 3;
pub const TAG_EXT_VAL: u8 = 4;
pub const TAG_COMB: u8 = 5;
pub const TAG_EXT_FN: u8 = 6;

pub const EXT_TYS: &[&str] = &["ivm:n32", "ivm:branch"];
pub const EXT_FNS: &[&str] = &["vi:n32:add", "vi:n32:sub", "vi:n32:eq", "vi:n32:ne", "ivm:branch"];

pub fn port(tag: u8, label: u16, payload: u64) -> u64 {
  assert!(tag < 8 && payload < 1 << 45);
  u64::from(tag) | (payload << 3) | (u64::from(label) << 48)
}

pub fn binary(tag: u8, label: u16, registers: [usize; 3]) -> u64 {
  assert!(tag < 8 && registers.iter().all(|&reg| reg < MAX_REGISTERS));
  1 | (u64::from(tag) << 1)
    | (u64::from(label) << 4)
    | ((registers[0] as u64) << 20)
    | ((registers[1] as u64) << (20 + ADDR_WIDTH))
    | ((registers[2] as u64) << (20 + 2 * ADDR_WIDTH))
}

pub fn nilary(register: usize) -> u64 {
  assert!(register < MAX_REGISTERS);
  (register as u64) << 20
}

pub fn graft(registers: usize, error: bool, instructions: usize) -> u64 {
  assert!(registers > 0 && registers <= MAX_REGISTERS);
  assert!(u32::try_from(instructions).is_ok());
  registers as u64 | (u64::from(error) << 31) | ((instructions as u64) << 32)
}
