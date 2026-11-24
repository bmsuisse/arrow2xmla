//! XML escaping, byte-for-byte what Python's `xml.sax.saxutils` does (the reference the tests compare against).

/// `saxutils.escape`: `&`, `<`, `>`.
pub fn escape_into(out: &mut Vec<u8>, s: &str) {
    let bytes = s.as_bytes();
    let mut start = 0;
    for (i, &b) in bytes.iter().enumerate() {
        let rep: &[u8] = match b {
            b'&' => b"&amp;",
            b'<' => b"&lt;",
            b'>' => b"&gt;",
            _ => continue,
        };
        out.extend_from_slice(&bytes[start..i]);
        out.extend_from_slice(rep);
        start = i + 1;
    }
    out.extend_from_slice(&bytes[start..]);
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
