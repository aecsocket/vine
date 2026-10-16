/// Grows the stack on demand to prevent stack overflow. Has a little overhead.
#[inline]
pub fn ensure_sufficient_stack<T>(f: impl FnOnce() -> T) -> T {
  #[cfg(feature = "std")]
  {
    const RED_ZONE_SIZE: usize = 32 * 1024; // 32KB
    const MIN_REALLOC_SIZE: usize = 1024 * 1024; // 1MB

    stacker::maybe_grow(RED_ZONE_SIZE, MIN_REALLOC_SIZE, f)
  }
  #[cfg(not(feature = "std"))]
  {
    // TODO: I don't know if this is safe exactly,
    // and on what platforms this is safe/unsafe.
    f()
  }
}
