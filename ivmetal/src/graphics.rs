//! An `embedded-graphics` adapter for the UEFI GOP framebuffer.
//!
//! Drawing updates a RAM backbuffer; `GopDisplay::present` copies a completed
//! frame into the memory-mapped Graphics Output Protocol framebuffer.
//! The adapter supports the direct-access
//! pixel formats (`RGB` and `BGR`); `BitMask`/`BltOnly` framebuffers (where
//! the CPU cannot meaningfully write pixels) are rejected at construction.
//!
//! ## Safety
//! The display holds a raw pointer into the framebuffer, which stays valid as
//! long as the GOP protocol instance is open and boot services have not been
//! exited. `ivmetal` never exits boot services and keeps the protocol open for
//! the lifetime of the program, so a `GopDisplay` may live arbitrarily long,
//! but a `GopDisplay` must not be used after `ExitBootServices`.

extern crate alloc;

use alloc::vec::Vec;
use core::{convert::Infallible, ptr};

use embedded_graphics_core::{
  draw_target::DrawTarget,
  pixelcolor::{Rgb888, RgbColor},
  prelude::*,
  primitives::Rectangle,
};
use uefi::{
  Status,
  proto::console::gop::{GraphicsOutput, PixelFormat},
};

/// A buffered `embedded-graphics` draw target over the GOP framebuffer.
pub struct GopDisplay {
  ptr: *mut u8,
  backbuffer: Vec<[u8; 4]>,
  width: usize,
  height: usize,
  stride: usize,
  format: PixelFormat,
}

impl GopDisplay {
  /// Creates a display from an open GOP protocol.
  pub fn new(gop: &mut GraphicsOutput) -> uefi::Result<Self> {
    let mode = gop.current_mode_info();
    let (width, height) = mode.resolution();
    let format = mode.pixel_format();
    if !matches!(format, PixelFormat::Rgb | PixelFormat::Bgr) {
      return Err(Status::UNSUPPORTED.into());
    }

    let stride = mode.stride();
    let framebuffer_size = stride
      .checked_mul(height)
      .and_then(|pixels| pixels.checked_mul(4))
      .ok_or(uefi::Error::from(Status::BAD_BUFFER_SIZE))?;

    let pixels = width.checked_mul(height).ok_or(uefi::Error::from(Status::OUT_OF_RESOURCES))?;
    let mut fb = gop.frame_buffer();
    if stride < width || framebuffer_size > fb.size() {
      return Err(Status::BAD_BUFFER_SIZE.into());
    }

    let mut backbuffer = Vec::new();
    backbuffer
      .try_reserve_exact(pixels)
      .map_err(|_| uefi::Error::from(Status::OUT_OF_RESOURCES))?;
    backbuffer.resize(pixels, [0; 4]);

    Ok(GopDisplay { ptr: fb.as_mut_ptr(), backbuffer, width, height, stride, format })
  }

  pub fn width(&self) -> usize {
    self.width
  }

  pub fn height(&self) -> usize {
    self.height
  }

  /// Copies the completed backbuffer to the display without clearing it.
  /// GOP provides no vsync, so this copy is not guaranteed to be tear-free.
  pub fn present(&mut self) {
    for y in 0..self.height {
      let row = &self.backbuffer[y * self.width..(y + 1) * self.width];
      for (x, pixel) in row.iter().enumerate() {
        // The framebuffer stride is in pixels; the RAM buffer is tightly packed.
        let offset = (y * self.stride + x) * 4;
        for (index, &byte) in pixel.iter().enumerate() {
          // Construction validates the framebuffer size and format.
          unsafe { ptr::write_volatile(self.ptr.add(offset + index), byte) };
        }
      }
    }
  }

  fn pixel_bytes(&self, color: Rgb888) -> [u8; 4] {
    match self.format {
      PixelFormat::Bgr => [color.b(), color.g(), color.r(), 0],
      PixelFormat::Rgb => [color.r(), color.g(), color.b(), 0],
      // Rejected in `new`; unreachable.
      PixelFormat::Bitmask | PixelFormat::BltOnly => unreachable!(),
    }
  }

  /// Writes to RAM, silently ignoring out-of-bounds coordinates as required
  /// by `DrawTarget::draw_iter`.
  fn write_pixel(&mut self, x: usize, y: usize, color: Rgb888) {
    if x < self.width && y < self.height {
      self.backbuffer[y * self.width + x] = self.pixel_bytes(color);
    }
  }

  /// Fills a clipped rectangle with a single color.
  fn fill_rect(&mut self, rect: Rectangle, color: Rgb888) {
    let bytes = self.pixel_bytes(color);
    let rect = rect.intersection(&Rectangle::new(
      Point::new(0, 0),
      Size::new(self.width as u32, self.height as u32),
    ));
    if rect.size.width == 0 || rect.size.height == 0 {
      return;
    }
    let top = rect.top_left.y.max(0) as usize;
    let left = rect.top_left.x.max(0) as usize;
    let bottom = (top + rect.size.height as usize).min(self.height);
    let right = (left + rect.size.width as usize).min(self.width);
    for y in top..bottom {
      self.backbuffer[y * self.width + left..y * self.width + right].fill(bytes);
    }
  }
}

impl DrawTarget for GopDisplay {
  type Color = Rgb888;
  type Error = Infallible;

  fn draw_iter<I>(&mut self, pixels: I) -> Result<(), Infallible>
  where
    I: IntoIterator<Item = Pixel<Self::Color>>,
  {
    for Pixel(Point { x, y }, color) in pixels {
      if x >= 0 && y >= 0 {
        self.write_pixel(x as usize, y as usize, color);
      }
    }
    Ok(())
  }

  fn fill_solid(&mut self, area: &Rectangle, color: Self::Color) -> Result<(), Infallible> {
    self.fill_rect(*area, color);
    Ok(())
  }

  fn clear(&mut self, color: Self::Color) -> Result<(), Infallible> {
    let bytes = self.pixel_bytes(color);
    self.backbuffer.fill(bytes);
    Ok(())
  }
}

impl OriginDimensions for GopDisplay {
  fn size(&self) -> Size {
    Size::new(self.width as u32, self.height as u32)
  }
}
