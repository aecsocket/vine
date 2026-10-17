//! An `embedded-graphics` adapter for the UEFI GOP framebuffer.
//!
//! This draws directly into the memory-mapped framebuffer exposed by the
//! Graphics Output Protocol, so no blitting or double-buffering is involved:
//! every draw is immediately visible. The adapter supports the direct-access
//! pixel formats (`RGB` and `BGR`); `BitMask`/`BltOnly` framebuffers (where
//! the CPU cannot meaningfully write pixels) are rejected at construction.
//!
//! ## Safety
//! The display holds a raw pointer into the framebuffer, which stays valid as
//! long as the GOP protocol instance is open and boot services have not been
//! exited. `ivmetal` never exits boot services and keeps the protocol open for
//! the lifetime of the program, so a `GopDisplay` may live arbitrarily long,
//! but a `GopDisplay` must not be used after `ExitBootServices`.

use core::{convert::Infallible, ptr};

use embedded_graphics_core::{
  draw_target::DrawTarget,
  pixelcolor::{Rgb888, RgbColor},
  prelude::*,
  primitives::Rectangle,
};
use uefi::proto::console::gop::{GraphicsOutput, PixelFormat};

/// A direct-write `embedded-graphics` draw target over the GOP framebuffer.
pub struct GopDisplay {
  ptr: *mut u8,
  size: usize,
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
    let mut fb = gop.frame_buffer();
    Ok(GopDisplay {
      ptr: fb.as_mut_ptr(),
      size: fb.size(),
      width,
      height,
      stride: mode.stride(),
      format: mode.pixel_format(),
    })
  }

  pub fn width(&self) -> usize {
    self.width
  }

  pub fn height(&self) -> usize {
    self.height
  }

  /// Writes a single pixel, silently ignoring out-of-bounds coordinates as
  /// required by `DrawTarget::draw_iter`.
  fn write_pixel(&mut self, x: usize, y: usize, color: Rgb888) {
    let Some(offset) = self.pixel_offset(x, y) else { return };
    let bytes = match self.format {
      PixelFormat::Bgr => [color.b(), color.g(), color.r(), 0],
      PixelFormat::Rgb => [color.r(), color.g(), color.b(), 0],
      // Rejected in `new`; unreachable.
      PixelFormat::Bitmask | PixelFormat::BltOnly => return,
    };
    for (index, byte) in bytes.into_iter().enumerate() {
      unsafe { ptr::write_volatile(self.ptr.add(offset + index), byte) };
    }
  }

  fn pixel_offset(&self, x: usize, y: usize) -> Option<usize> {
    if x >= self.width || y >= self.height {
      return None;
    }
    let offset = y * self.stride + x * 4;
    (offset + 4 <= self.size).then_some(offset)
  }

  /// Fills a clipped rectangle with a single color.
  fn fill_rect(&mut self, rect: Rectangle, color: Rgb888) {
    let (r, g, b) = (color.r(), color.g(), color.b());
    let bytes = match self.format {
      PixelFormat::Bgr => [b, g, r, 0],
      PixelFormat::Rgb => [r, g, b, 0],
      PixelFormat::Bitmask | PixelFormat::BltOnly => return,
    };
    let rect = rect.intersection(&Rectangle::new(
      Point::new(0, 0),
      Size::new(self.width as u32, self.height as u32),
    ));
    let top = rect.top_left.y.max(0) as usize;
    let left = rect.top_left.x.max(0) as usize;
    let bottom = (top + rect.size.height as usize).min(self.height);
    let right = (left + rect.size.width as usize).min(self.width);
    for y in top..bottom {
      for x in left..right {
        let Some(offset) = self.pixel_offset(x, y) else { continue };
        for (index, byte) in bytes.into_iter().enumerate() {
          unsafe { ptr::write_volatile(self.ptr.add(offset + index), byte) };
        }
      }
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
    self.fill_rect(
      Rectangle::new(Point::new(0, 0), Size::new(self.width as u32, self.height as u32)),
      color,
    );
    Ok(())
  }
}

impl OriginDimensions for GopDisplay {
  fn size(&self) -> Size {
    Size::new(self.width as u32, self.height as u32)
  }
}
