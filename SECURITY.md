# Security

## Threat model

- **Request bodies (`parse_request`) are untrusted** (they arrive over the network). The parser refuses any DOCTYPE
  (no entity expansion), bounds nesting (64) and the number of elements (100 000), and refuses what a strict XML parser
  refuses. A body longer than `max_size` (default 4 MiB) is refused before it is read (an XMLA
  request is a few kB; a statement with many filters, a few hundred kB): lower it for a public endpoint.
- **Arrow arrays are trusted.** The Arrow C data interface does not say how long a buffer is: a variable-size array's
  extent is read from its own offsets. The binding refuses what the structs themselves show to be wrong (negative
  sizes, missing buffers, released arrays, offsets that go down or leave the data, text that is not UTF-8, a validity
  bitmap that is too short) but cannot detect a producer that lies about its buffers. Do not pass arrays built from
  memory you do not trust.
- **The answer is always well-formed XML**: characters XML 1.0 forbids in data become U+FFFD, and markup characters are
  escaped.

## Reporting

Please report a vulnerability privately to the maintainers (GitHub security advisories on this repository), not in a
public issue.
