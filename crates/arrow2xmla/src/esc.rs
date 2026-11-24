//! XML escaping: what Python's `xml.sax.saxutils` does (`&`, `<`, `>`, and `quoteattr` for attributes), plus one
//! deliberate difference: a character XML 1.0 forbids becomes U+FFFD, so the answer is always well-formed.

/// `saxutils.escape` (`&`, `<`, `>`), a carriage return as a reference (a parser would read a raw one as a line
/// feed), and a character XML 1.0 forbids becomes U+FFFD.
pub fn escape_into(out: &mut Vec<u8>, s: &str) {
    escape_bytes_into(out, s.as_bytes());
}

/// The replacement for a character XML 1.0 forbids (U+FFFD): a document holding one is not well-formed, so a client's
/// parser (Excel's included) would refuse the whole answer.
const REPLACEMENT: &[u8] = "\u{FFFD}".as_bytes();

/// How many bytes at the start of `rest` are a character XML 1.0 forbids (0 when the first one is fine): a control
/// character other than tab, line feed and carriage return, or U+FFFE / U+FFFF. (Surrogates cannot occur in UTF-8.)
#[inline]
fn forbidden(rest: &[u8]) -> usize {
    match rest[0] {
        0x00..=0x08 | 0x0B | 0x0C | 0x0E..=0x1F => 1,
        0xEF if rest.len() >= 3 && rest[1] == 0xBF && (rest[2] == 0xBE || rest[2] == 0xBF) => 3,
        _ => 0,
    }
}

/// True when `bytes` hold a character XML 1.0 forbids (a control character other than tab, line feed and carriage
/// return, or U+FFFE / U+FFFF).
#[cfg(feature = "request")]
pub fn has_forbidden(bytes: &[u8]) -> bool {
    let mut i = 0;
    while i < bytes.len() {
        if forbidden(&bytes[i..]) > 0 {
            return true;
        }
        i += 1;
    }
    false
}

/// True when `bytes` may hold a character `escape_bytes_into` changes: `&`, `<`, `>`, `\r`, or one XML forbids. May say
/// yes for a text that needs nothing (0xEF starts many characters); never says no when something is to change.
#[inline]
pub fn needs_escape(bytes: &[u8]) -> bool {
    bytes.iter().any(|&b| matches!(b, b'&' | b'<' | b'>' | b'\r' | 0x00..=0x08 | 0x0B | 0x0C | 0x0E..=0x1F | 0xEF))
}

/// `escape` on UTF-8 bytes (the bytes of an Arrow string): a run with nothing to change is copied whole.
pub fn escape_bytes_into(out: &mut Vec<u8>, bytes: &[u8]) {
    let mut start = 0;
    let mut i = 0;
    while i < bytes.len() {
        let (rep, len): (&[u8], usize) = match bytes[i] {
            b'&' => (b"&amp;", 1),
            b'<' => (b"&lt;", 1),
            b'>' => (b"&gt;", 1),
            b'\r' => (b"&#13;", 1), // a raw carriage return would be read back as a line feed
            _ => match forbidden(&bytes[i..]) {
                0 => {
                    i += 1;
                    continue;
                }
                n => (REPLACEMENT, n),
            },
        };
        out.extend_from_slice(&bytes[start..i]);
        out.extend_from_slice(rep);
        i += len;
        start = i;
    }
    out.extend_from_slice(&bytes[start..]);
}

/// An attribute value to go between double quotes: escaped, with `"` and the whitespace a parser would normalise
/// written as references.
pub fn escape_attr_into(out: &mut Vec<u8>, s: &str) {
    let mut e = Vec::with_capacity(s.len() + 8);
    escape_into(&mut e, s);
    for &b in &e {
        match b {
            b'"' => out.extend_from_slice(b"&quot;"),
            b'\n' => out.extend_from_slice(b"&#10;"),
            b'\r' => out.extend_from_slice(b"&#13;"),
            b'\t' => out.extend_from_slice(b"&#9;"),
            _ => out.push(b),
        }
    }
}

/// `saxutils.quoteattr`: an attribute value with its quotes (`"` unless the value holds `"` and no `'`).
pub fn quote_attr_into(out: &mut Vec<u8>, s: &str) {
    let mut esc = Vec::with_capacity(s.len() + 8);
    escape_into(&mut esc, s);
    let esc = String::from_utf8(esc).expect("escaped utf-8 stays utf-8");
    let esc = esc.replace('\n', "&#10;").replace('\r', "&#13;").replace('\t', "&#9;");
    if esc.contains('"') {
        if esc.contains('\'') {
            out.push(b'"');
            out.extend_from_slice(esc.replace('"', "&quot;").as_bytes());
            out.push(b'"');
        } else {
            out.push(b'\'');
            out.extend_from_slice(esc.as_bytes());
            out.push(b'\'');
        }
    } else {
        out.push(b'"');
        out.extend_from_slice(esc.as_bytes());
        out.push(b'"');
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn e(s: &str) -> String {
        let mut v = Vec::new();
        escape_into(&mut v, s);
        String::from_utf8(v).unwrap()
    }
    fn q(s: &str) -> String {
        let mut v = Vec::new();
        quote_attr_into(&mut v, s);
        String::from_utf8(v).unwrap()
    }

    #[test]
    fn forbidden_characters_become_the_replacement_character() {
        assert_eq!(e("a\u{0}b\u{1F}c\u{B}"), "a\u{FFFD}b\u{FFFD}c\u{FFFD}");
        assert_eq!(e("x\u{FFFE}y\u{FFFF}"), "x\u{FFFD}y\u{FFFD}");
        assert_eq!(e("tab\tnl\ncr\r"), "tab\tnl\ncr&#13;"); // tab and line feed are kept; a carriage return survives a parse
        assert_eq!(e("\u{FFFD}\u{FF01}\u{F000}"), "\u{FFFD}\u{FF01}\u{F000}"); // legal, and share a lead byte
    }

    #[test]
    fn attribute_values() {
        let mut v = Vec::new();
        escape_attr_into(&mut v, "a\"b<c>\n\u{0}");
        assert_eq!(String::from_utf8(v).unwrap(), "a&quot;b&lt;c&gt;&#10;\u{FFFD}");
    }

    #[test]
    fn escapes_the_three() {
        assert_eq!(e("a&b<c>d"), "a&amp;b&lt;c&gt;d");
        assert_eq!(e("plain"), "plain");
        assert_eq!(e("é\u{1F600}"), "é\u{1F600}");
    }

    #[test]
    fn quotes_like_saxutils() {
        assert_eq!(q("a"), "\"a\"");
        assert_eq!(q("a\"b"), "'a\"b'");
        assert_eq!(q("a\"b'c"), "\"a&quot;b'c\"");
        assert_eq!(q("x\ny\tz\r"), "\"x&#10;y&#9;z&#13;\"");
    }
}
