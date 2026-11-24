"""Fetch the XMLA schemas to check answers against: `uv run --with lxml python tests/e2e/schemas.py [DIR]`.

The schemas are Microsoft's XMLA 1.1 / Analysis Services set (urn:schemas-microsoft-com:xml-analysis:*), taken from a
public mirror (github.com/eclipse-daanse/legacy.dashboard.client, public/def) rather than vendored here, so their
licence stays with their source. The mirror's mddataset schema is a lossy copy, so two things are patched:

- `elementFormDefault="qualified"` is added (the document's elements are all in the mddataset namespace), and
- the same for the rowset schema (`rowset-patched.xsd`), whose row wildcard is made lax (the columns are declared in the
  inline schema block of the answer, which the mirror's copy leaves out), and
- the `Value` element of a cell is dropped from the content model (as written it is ambiguous with the wildcard next
  to it, and it cannot hold text): a cell's `Value` is then matched by the wildcard, and `CellOrdinal` stays checked.

What validates is therefore the structure (OlapInfo, Axes, CellData, their order and nesting) and the cell ordinals,
not the typed values.
"""

import re
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/eclipse-daanse/legacy.dashboard.client/main/public/def/"
FILES = ["xmla-ds.xsd", "xmla-rs.xsd", "msxmla.xsd", "xmla-x.xsd", "eng.xsd", "eng100.xsd", "eng100_100.xsd", "eng200.xsd", "eng200_200.xsd", "eng300.xsd", "eng300_300.xsd", "eng400.xsd", "eng600.xsd", "eng800.xsd", "ext.xsd", "eng2.xsd", "eng2_2.xsd"]


def fetch(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        target = directory / name
        if not target.exists():
            target.write_bytes(urllib.request.urlopen(BASE + name, timeout=30).read())
    text = (directory / "xmla-ds.xsd").read_text()
    text = text.replace('<xsd:schema version="1.0"', '<xsd:schema version="1.0" elementFormDefault="qualified"', 1)
    text = re.sub(r'<xsd:element name="Value" minOccurs="0" maxOccurs="1">.*?</xsd:element>\s*(?=<xsd:any)', "", text, count=1, flags=re.S)
    patched = directory / "mddataset-patched.xsd"
    patched.write_text(text)
    rs = (directory / "xmla-rs.xsd").read_text().replace('<xsd:schema ', '<xsd:schema elementFormDefault="qualified" ', 1).replace('<xsd:any ', '<xsd:any processContents="skip" ')
    (directory / "rowset-patched.xsd").write_text(rs)
    return patched


if __name__ == "__main__":
    print(fetch(Path(sys.argv[1] if len(sys.argv) > 1 else ".schema-cache")))
