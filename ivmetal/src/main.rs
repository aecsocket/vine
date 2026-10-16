#![no_std]
#![no_main]

use core::time::Duration;

use log::info;
use uefi::prelude::*;

#[entry]
fn main() -> Status {
  uefi::helpers::init().unwrap();
  info!("hello world!");
  boot::stall(Duration::from_secs(10));

  Status::SUCCESS
}
