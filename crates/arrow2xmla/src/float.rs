//! A float as its shortest round-trip text in Python's `repr` style (`1e-05`, `1e+16`, `2.0`, `nan`, `inf`): the
//! text clients of an existing XMLA server already get, so a switch to this crate changes no byte.

use std::fmt::Write;

pub fn write_f64(out: &mut Vec<u8>, v: f64) {
    if v.is_nan() {
        out.extend_from_slice(b"nan");
        return;
    }
    if v.is_infinite() {
        out.extend_from_slice(if v < 0.0 { b"-inf" } else { b"inf" });
        return;
    }
    // Rust's `{:e}` is the shortest digits that round-trip: `-1.2345e-5`, `0e0`.
    let mut s = String::with_capacity(32);
    let _ = write!(s, "{v:e}");
    let (mantissa, exp) = s.split_once('e').expect("LowerExp has an exponent");
    let exp: i32 = exp.parse().expect("exponent");
    let (neg, mantissa) = match mantissa.strip_prefix('-') {
        Some(m) => (true, m),
        None => (false, mantissa),
    };
    if neg {
        out.push(b'-');
    }
    let mut digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let mut exp = exp;
    // Shortest digits can tie between two candidates (`...521.25`: `.2` or `.3`); Python's repr takes the even one,
    // Rust the upper. A tie needs an exact decimal of at most 18 digits, so only a value with at most 17 binary
    // fraction places and 16+ digits is looked at: the correctly rounded (half-even) text at the same length, kept
    // when it reads back as the same float.
    if digits.len() >= 16 && (v * 131072.0).fract() == 0.0 {
        let mut t = String::with_capacity(32);
        let _ = write!(t, "{:.*e}", digits.len() - 1, v.abs());
        if let Some((m, e)) = t.split_once('e') {
            let d: String = m.chars().filter(|c| *c != '.').collect();
            if d != digits && t.parse::<f64>().map(|x| x == v.abs()).unwrap_or(false) {
                digits = d;
                exp = e.parse().unwrap_or(exp);
            }
        }
    }
    if digits == "0" {
        out.extend_from_slice(b"0.0");
        return;
    }
    let digits = digits.as_bytes();
    if (-4..16).contains(&exp) {
        if exp >= 0 {
            let int_len = exp as usize + 1;
            if digits.len() <= int_len {
                out.extend_from_slice(digits);
                out.resize(out.len() + (int_len - digits.len()), b'0');
                out.extend_from_slice(b".0");
            } else {
                out.extend_from_slice(&digits[..int_len]);
                out.push(b'.');
                out.extend_from_slice(&digits[int_len..]);
            }
        } else {
            out.extend_from_slice(b"0.");
            out.resize(out.len() + (-exp - 1) as usize, b'0');
            out.extend_from_slice(digits);
        }
    } else {
        out.push(digits[0]);
        if digits.len() > 1 {
            out.push(b'.');
            out.extend_from_slice(&digits[1..]);
        }
        out.push(b'e');
        out.push(if exp < 0 { b'-' } else { b'+' });
        let a = exp.unsigned_abs();
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
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.5e-7, "1.5e-07"),
            (1.0e-100, "1e-100"),
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
