//! A float as its shortest round-trip text in Python's `repr` style (`1e-05`, `1e+16`, `2.0`, `nan`, `inf`): the text
//! clients of an existing XMLA server already get, so a switch to this crate changes no byte. The digits come from
//! `zmij` (shortest, nearest, ties to even); the common range needs no change, the rest is laid out here.

pub fn write_f64(out: &mut Vec<u8>, v: f64) {
    if v.is_nan() {
        out.extend_from_slice(b"nan");
        return;
    }
    if v.is_infinite() {
        out.extend_from_slice(if v < 0.0 { b"-inf" } else { b"inf" });
        return;
    }
    let mut buf = zmij::Buffer::new();
    let s = buf.format_finite(v).as_bytes();
    // fixed notation from 1e-4 to 1e16 is the same text in both
    if (v == 0.0 || v.abs() >= 1e-4) && !s.contains(&b'e') {
        out.extend_from_slice(s);
        return;
    }
    relayout(out, s);
}

/// `s` is zmij's text of a finite non-zero float outside the common range: digits and exponent are read from it and
/// laid out as Python does (`1e-05`, `1.5e-07`, `9.999e-05`, `1e+16`).
fn relayout(out: &mut Vec<u8>, s: &[u8]) {
    let (neg, s) = match s.strip_prefix(b"-") {
        Some(r) => (true, r),
        None => (false, s),
    };
    let (mantissa, exp) = match s.iter().position(|&b| b == b'e') {
        Some(i) => (&s[..i], std::str::from_utf8(&s[i + 1..]).ok().and_then(|t| t.parse::<i32>().ok()).unwrap_or(0)),
        None => (s, 0),
    };
    let dot = mantissa.iter().position(|&b| b == b'.').unwrap_or(mantissa.len());
    let mut digits: Vec<u8> = mantissa.iter().copied().filter(|b| *b != b'.').collect();
    let lead = digits.iter().take_while(|&&b| b == b'0').count();
    digits.drain(..lead);
    while digits.last() == Some(&b'0') {
        digits.pop();
    }
    if digits.is_empty() {
        out.extend_from_slice(if neg { b"-0.0" } else { b"0.0" });
        return;
    }
    // the decimal exponent of the first digit
    let exp10 = exp + dot as i32 - 1 - lead as i32;
    if neg {
        out.push(b'-');
    }
    if (-4..16).contains(&exp10) {
        if exp10 >= 0 {
            let int_len = exp10 as usize + 1;
            if digits.len() <= int_len {
                out.extend_from_slice(&digits);
                out.resize(out.len() + (int_len - digits.len()), b'0');
                out.extend_from_slice(b".0");
            } else {
                out.extend_from_slice(&digits[..int_len]);
                out.push(b'.');
                out.extend_from_slice(&digits[int_len..]);
            }
        } else {
            out.extend_from_slice(b"0.");
            out.resize(out.len() + (-exp10 - 1) as usize, b'0');
            out.extend_from_slice(&digits);
        }
    } else {
        out.push(digits[0]);
        if digits.len() > 1 {
            out.push(b'.');
            out.extend_from_slice(&digits[1..]);
        }
        out.push(b'e');
        out.push(if exp10 < 0 { b'-' } else { b'+' });
        let a = exp10.unsigned_abs();
        if a < 10 {
            out.push(b'0');
        }
        out.extend_from_slice(itoa::Buffer::new().format(a).as_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r(v: f64) -> String {
        let mut o = Vec::new();
        write_f64(&mut o, v);
        String::from_utf8(o).unwrap()
    }

    #[test]
    #[allow(clippy::excessive_precision)] // exact ties: 686036261402521.25 is representable
    fn matches_python_repr() {
        for (v, want) in [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (-2.5, "-2.5"),
            (123.456, "123.456"),
            (100.0, "100.0"),
            (1e15, "1000000000000000.0"),
            (1e16, "1e+16"),
            (1.5e16, "1.5e+16"),
            (-1.5e16, "-1.5e+16"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (0.00009999, "9.999e-05"),
            (-0.00001234, "-1.234e-05"),
            (1.5e-7, "1.5e-07"),
            (1.0e-100, "1e-100"),
            (5e-324, "5e-324"),
            (f64::MAX, "1.7976931348623157e+308"),
            (0.1 + 0.2, "0.30000000000000004"),
            (686036261402521.25, "686036261402521.2"),
            (686036261402521.75, "686036261402521.8"),
            (f64::NAN, "nan"),
            (f64::INFINITY, "inf"),
            (f64::NEG_INFINITY, "-inf"),
        ] {
            assert_eq!(r(v), want, "{v:?}");
        }
    }
}
