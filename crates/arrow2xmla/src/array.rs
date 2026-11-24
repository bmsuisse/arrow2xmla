//! Zero-copy views of columnar data: what the writer reads. A view is a few slices over memory the caller owns (Arrow
//! buffers, a Rust `Vec`): nothing here depends on an Arrow library. A binding builds them from the Arrow C data
//! interface; with the `arrow` feature an `arrow_array::ArrayRef` converts too.

use crate::Error;

/// The values of a column, as the slices they are stored in (already shifted by the array's offset).
#[derive(Clone, Copy, Debug)]
#[non_exhaustive]
pub enum Values<'a> {
    /// Half-precision floats as their bits.
    F16(&'a [u16]),
    F64(&'a [f64]),
    F32(&'a [f32]),
    I64(&'a [i64]),
    I32(&'a [i32]),
    I16(&'a [i16]),
    I8(&'a [i8]),
    U64(&'a [u64]),
    U32(&'a [u32]),
    U16(&'a [u16]),
    U8(&'a [u8]),
    /// Text with `i32` offsets (`offsets.len() == len + 1`).
    Utf8 { offsets: &'a [i32], data: &'a [u8] },
    /// Text with `i64` offsets.
    LargeUtf8 { offsets: &'a [i64], data: &'a [u8] },
    /// Text as 16-byte views (inline up to 12 bytes, else a buffer index and offset) over `buffers`.
    Utf8View { views: &'a [[u8; 16]], buffers: &'a [&'a [u8]] },
}

/// Which rows have a value: bit `i + offset` of `bits` (LSB first). Absent: all rows do.
#[derive(Clone, Copy, Debug)]
pub struct Validity<'a> {
    pub bits: &'a [u8],
    pub offset: usize,
}

#[derive(Clone, Copy, Debug)]
pub struct Array<'a> {
    pub values: Values<'a>,
    pub validity: Option<Validity<'a>>,
}

impl<'a> Array<'a> {
    pub fn new(values: Values<'a>) -> Self {
        Self { values, validity: None }
    }

    pub fn with_validity(mut self, bits: &'a [u8], offset: usize) -> Self {
        self.validity = Some(Validity { bits, offset });
        self
    }

    pub fn len(&self) -> usize {
        match self.values {
            Values::F16(v) => v.len(),
            Values::F64(v) => v.len(),
            Values::F32(v) => v.len(),
            Values::I64(v) => v.len(),
            Values::I32(v) => v.len(),
            Values::I16(v) => v.len(),
            Values::I8(v) => v.len(),
            Values::U64(v) => v.len(),
            Values::U32(v) => v.len(),
            Values::U16(v) => v.len(),
            Values::U8(v) => v.len(),
            Values::Utf8 { offsets, .. } => offsets.len().saturating_sub(1),
            Values::LargeUtf8 { offsets, .. } => offsets.len().saturating_sub(1),
            Values::Utf8View { views, .. } => views.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    #[inline]
    pub fn is_valid(&self, i: usize) -> bool {
        match self.validity {
            None => true,
            Some(v) => {
                let b = v.offset + i;
                v.bits.get(b >> 3).is_some_and(|byte| byte >> (b & 7) & 1 == 1)
            }
        }
    }

    pub fn is_text(&self) -> bool {
        matches!(self.values, Values::Utf8 { .. } | Values::LargeUtf8 { .. } | Values::Utf8View { .. })
    }

    /// The bytes of a text value (valid UTF-8 by the Arrow contract); None for a null or a non-text array.
    #[inline]
    pub fn text(&self, i: usize) -> Option<&'a [u8]> {
        if !self.is_valid(i) {
            return None;
        }
        match self.values {
            Values::Utf8 { offsets, data } => data.get(offsets[i] as usize..offsets[i + 1] as usize),
            Values::LargeUtf8 { offsets, data } => data.get(offsets[i] as usize..offsets[i + 1] as usize),
            Values::Utf8View { views, buffers } => {
                let v = &views[i];
                let len = u32::from_le_bytes([v[0], v[1], v[2], v[3]]) as usize;
                if len <= 12 {
                    Some(&v[4..4 + len])
                } else {
                    let buf = u32::from_le_bytes([v[8], v[9], v[10], v[11]]) as usize;
                    let off = u32::from_le_bytes([v[12], v[13], v[14], v[15]]) as usize;
                    buffers.get(buf)?.get(off..off + len)
                }
            }
            _ => None,
        }
    }

    /// Checks what the writer relies on: offsets that stay inside the data and rise, views inside their buffers, text
    /// that is valid UTF-8 (a string that is not would put bytes in the answer no XML parser accepts), a validity
    /// bitmap long enough. The buffers themselves are trusted to be as long as they say.
    pub fn check(&self) -> Result<(), Error> {
        fn offsets_ok<T: Copy + Into<i64>>(offsets: &[T], data: usize) -> bool {
            let mut prev = 0i64;
            offsets.iter().all(|&o| {
                let o: i64 = o.into();
                let ok = o >= prev && o as usize <= data;
                prev = o;
                ok
            })
        }
        let bad = |what: &str| Err(Error::Invalid(format!("a malformed array: {what}")));
        match self.values {
            Values::Utf8 { offsets, data } => {
                if !offsets_ok(offsets, data.len()) {
                    return bad("text offsets");
                }
                if std::str::from_utf8(data).is_err() {
                    return bad("text that is not valid UTF-8");
                }
            }
            Values::LargeUtf8 { offsets, data } => {
                if !offsets_ok(offsets, data.len()) {
                    return bad("text offsets");
                }
                if std::str::from_utf8(data).is_err() {
                    return bad("text that is not valid UTF-8");
                }
            }
            Values::Utf8View { .. } => {
                for i in 0..self.len() {
                    if self.is_valid(i) {
                        match self.text(i) {
                            Some(t) if std::str::from_utf8(t).is_ok() => {}
                            Some(_) => return bad("text that is not valid UTF-8"),
                            None => return bad("a string view outside its buffer"),
                        }
                    }
                }
            }
            _ => {}
        }
        if let Some(v) = self.validity {
            if v.bits.len() * 8 < v.offset + self.len() {
                return bad("validity bitmap too short");
            }
        }
        Ok(())
    }
}

#[cfg(feature = "arrow")]
mod arrow_impl {
    use super::*;
    use arrow_array::cast::AsArray;
    use arrow_array::types::*;
    use arrow_array::{Array as _, ArrayRef};
    use arrow_schema::DataType;

    impl<'a> TryFrom<&'a ArrayRef> for Array<'a> {
        type Error = Error;

        fn try_from(a: &'a ArrayRef) -> Result<Self, Error> {
            let values = match a.data_type() {
                DataType::Float64 => Values::F64(a.as_primitive::<Float64Type>().values()),
                DataType::Float32 => Values::F32(a.as_primitive::<Float32Type>().values()),
                DataType::Int64 => Values::I64(a.as_primitive::<Int64Type>().values()),
                DataType::Int32 => Values::I32(a.as_primitive::<Int32Type>().values()),
                DataType::Int16 => Values::I16(a.as_primitive::<Int16Type>().values()),
                DataType::Int8 => Values::I8(a.as_primitive::<Int8Type>().values()),
                DataType::UInt64 => Values::U64(a.as_primitive::<UInt64Type>().values()),
                DataType::UInt32 => Values::U32(a.as_primitive::<UInt32Type>().values()),
                DataType::UInt16 => Values::U16(a.as_primitive::<UInt16Type>().values()),
                DataType::UInt8 => Values::U8(a.as_primitive::<UInt8Type>().values()),
                DataType::Utf8 => {
                    let s = a.as_string::<i32>();
                    Values::Utf8 { offsets: s.value_offsets(), data: s.value_data() }
                }
                DataType::LargeUtf8 => {
                    let s = a.as_string::<i64>();
                    Values::LargeUtf8 { offsets: s.value_offsets(), data: s.value_data() }
                }
                t => return Err(Error::Unsupported(format!("an array of type {t}"))),
            };
            let mut out = Array::new(values);
            if let Some(n) = a.nulls() {
                let b = n.inner();
                out = out.with_validity(b.values(), b.offset());
            }
            Ok(out)
        }
    }
}
