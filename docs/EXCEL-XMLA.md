# What Excel asks of an XMLA endpoint

What a server must answer for an Excel PivotTable to connect, list its fields and show a pivot. It is read off the 23
requests recorded from a real PivotTable session (`tests/fixtures/excel`, Excel for Windows through MSOLAP), so it is
what Excel was seen to send, not the whole XMLA specification. What `arrow2xmla` covers is marked.

## The wire

- **SOAP 1.1 over HTTP `POST`**, `Content-Type: text/xml`. One request, one response, each a SOAP envelope. A refusal is
  an HTTP 500 with a `soap:Fault` (`faultcode`, `faultstring`).
- **Session**: Excel sends `BeginSession` once, then `<Session SessionId="..."/>` in the header of every later request,
  and `EndSession` when it is done. The server answers with a `<Session SessionId="..."/>` header: a new id for
  `BeginSession`, the client's own otherwise. Of the 23 recordings: 1 begin, 20 use, 1 end, 1 none.
  `parse_request` returns this as `Request.session`.
- Bodies are UTF-8 (UTF-16 with a BOM is accepted by the parser). No DOCTYPE, ever.

## The requests, in the order a session needs them

| Request | Seen | Restrictions Excel sends | The answer |
|---|---|---|---|
| `DISCOVER_PROPERTIES` | 3 | `PropertyName` | rowset of server properties |
| `DISCOVER_SCHEMA_ROWSETS` | 1 | – | the rowsets the server supports |
| `DISCOVER_LITERALS` | 1 | – | rowset |
| `DBSCHEMA_CATALOGS` | 1 | – | the catalogs (Excel's `Catalog` property then names one) |
| `MDSCHEMA_CUBES` | 2 | `CUBE_NAME` | the cubes |
| `MDSCHEMA_DIMENSIONS` | 1 | `CATALOG_NAME`, `CUBE_NAME` | rowset |
| `MDSCHEMA_HIERARCHIES` | 1 | `CATALOG_NAME`, `CUBE_NAME` | rowset |
| `MDSCHEMA_LEVELS` | 1 | `CATALOG_NAME`, `CUBE_NAME` | rowset |
| `MDSCHEMA_MEASURES` | 1 | `CUBE_NAME` | rowset |
| `MDSCHEMA_MEMBERS` | 1 | `CUBE_NAME`, `MEMBER_UNIQUE_NAME`, `TREE_OP` | rowset (drill-down uses `TREE_OP`) |
| `MDSCHEMA_PROPERTIES` | 1 | `PROPERTY_TYPE` | rowset |
| `Execute`, empty statement | 2 | – | an empty result: it only opens the session |
| `Execute`, MDX | 7 | – | **a cellset** |

Every request carries a `PropertyList`. Seen: `Catalog`, `LocaleIdentifier` (1033, 3072), `Content` (`SchemaData`),
`Format` (`Native`, `Multidimensional`, `Tabular`), `Dialect` (`MDX`), `Timeout`, `AxisFormat` (`TupleFormat`),
`MdxMissingMemberMode`, `DbpropMsmdMDXCompatibility`, `DbpropMsmdFlattened2`, `SafetyOptions`, `SspropInitAppName`,
`ClientProcessID`. A server can ignore most; `Catalog`, `Format` and `Content` change the answer.

## The cellset (`Execute`)

`Format=Native` or `Multidimensional` asks for an **MDDataset** (`urn:schemas-microsoft-com:xml-analysis:mddataset`):
`OlapInfo` (cube, one `AxisInfo` per axis and one for `SlicerAxis`, each listing its hierarchies and the member
properties written; `CellInfo` listing the cell properties), `Axes` (the tuples, each member with its unique name,
caption, level and `DisplayInfo`) and `CellData` (cells by ordinal).

This is what `Cellset.to_xml()` writes. How the pieces line up:

- Axis0 is the columns, Axis1 the rows; a cell's ordinal is `row * columns + column`; a cell with no value is absent.
- `DisplayInfo` carries the children count and the *drilled down* and *same parent* flags Excel uses to indent and to
  show the +/- buttons; `TupleAxis` works it out from the tuples.
- Cell values are typed by `xsi:type` (`xsd:int`, `xsd:long`, `xsd:unsignedLong`, `xsd:double`, `xsd:string`).
- The statements are MDX: `SELECT [NON EMPTY] ... ON COLUMNS, ... ON ROWS FROM [cube] WHERE ... CELL PROPERTIES ...`,
  `WITH MEMBER/SET`, `Hierarchize`, `DrilldownLevel`, `TopCount`, `Filter`, `Generate`, subselects. **Parsing and
  evaluating MDX is the server's job**; this package does not do it.

## What `arrow2xmla` covers, and what it does not

| Piece | Status |
|---|---|
| Request parsing: session, restrictions, properties, statement | done (`parse_request`) |
| MDDataset cellset | done (`Cellset`), byte-for-byte against a reference writer |
| SOAP envelope and fault around an answer | **not yet**: the example server in `tests/e2e/xmla_mock.py` shows the shape |
| Rowsets for the `Discover` requests above | **not yet** |
| MDX parser / evaluator | out of scope |
| Binary XML (`sx`) and Xpress compression, which MSOLAP can negotiate | not yet; plain XML is accepted |

## Open points: not verified against real Excel

- **Cell properties.** Excel asks `CELL PROPERTIES VALUE, FORMAT_STRING, LANGUAGE, BACK_COLOR, FORE_COLOR, FONT_FLAGS`.
  `arrow2xmla` writes only `VALUE`, `FORMAT_STRING` and `LANGUAGE` and refuses the others, so a server must declare what
  it writes. Whether Excel is content with a `CellInfo` that lists fewer than it asked for is expected but untested here.
- **Excel for Windows only.** Real Excel cannot be run in this repository's CI. The tests replay the recordings against a
  mock endpoint (`tests/e2e`) and read the answers the way a client does; that proves the answers are well-formed and
  carry the data, not that Excel renders them.
- **Power BI is untested.** The recordings are Excel's. Power BI's XMLA endpoint is a *server* (Analysis Services /
  Premium) that tools connect to; Power BI Desktop and the service speak other protocols to their own sources. Whether
  Power BI or other MSOLAP clients (SSMS, Tableau, ...) accept these answers has not been tried.

To record more: run a PivotTable against any endpoint and capture the HTTP bodies; each request goes into
`tests/fixtures/excel/` and is picked up by the parity and end-to-end tests.

## Official documents, and what we check against

- [XML for Analysis Specification](https://msdn.microsoft.com/en-us/library/ms977626(v=msdn.10)) (XMLA 1.0; Analysis
  Services is [compliant with 1.1](https://technet.microsoft.com/sqlserver/ms187125(v=sql.100)) and extends it).
- [MS-SSAS](https://learn.microsoft.com/en-us/openspecs/sql_server_protocols/MS-SSAS/68a9475e-27d6-413a-9786-95bb19652b19):
  the protocol document; it lists the `mddataset`, `rowset` and `exception` namespaces.
- [XMLA reference](https://learn.microsoft.com/analysis-services/xmla/xml-for-analysis-xmla-reference).

Checks that use them:

- **Schema validation** (`tests/e2e/test_schema.py`, optional: `uv run --with lxml pytest tests/e2e/test_schema.py`).
  Answers are validated against the `mddataset` schema, fetched from a public mirror rather than vendored
  (`tests/e2e/schemas.py`). The mirror's copy is lossy and is patched in two places, so this checks the structure and the
  cell ordinals, not the typed values; mutated answers are shown to fail.
- **Replay and read-back** (`tests/e2e`): the recorded Excel requests against a mock endpoint, answers read like a client.
- **Not done**: an independent client. The Python `xmla` package targets Python 2 and does not run on a current Python;
  olap4j (Java) would, and needs a JDK. Mondrian's XMLA tests hold expected request/response pairs (including for older
  Excel) that could be mined.
