//! The data model: what a cellset is made of. No engine, no product names: a caller fills these in from wherever its
//! members and numbers come from.

use crate::array::Array;

/// One member of a hierarchy (a value of a field, its All member, a measure).
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct Member {
    pub hierarchy: String,
    pub unique_name: String,
    pub caption: String,
    pub level_name: String,
    pub level_number: i32,
    /// "" for the All member and for a measure.
    pub parent_unique_name: String,
    pub children_cardinality: u32,
    /// MDSCHEMA MEMBER_TYPE: 1 a value, 2 the All member, 3 a measure, 4 a calculated member.
    pub member_type: u8,
}

/// A member property a client may ask for (`DIMENSION PROPERTIES`), written after each member of its axis.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum MemberProp {
    ParentUniqueName,
    HierarchyUniqueName,
    MemberUniqueName,
    MemberCaption,
    LevelUniqueName,
    LevelNumber,
    ChildrenCardinality,
    ParentLevel,
    ParentCount,
    MemberType,
}

impl MemberProp {
    pub fn name(self) -> &'static str {
        match self {
            Self::ParentUniqueName => "PARENT_UNIQUE_NAME",
            Self::HierarchyUniqueName => "HIERARCHY_UNIQUE_NAME",
            Self::MemberUniqueName => "MEMBER_UNIQUE_NAME",
            Self::MemberCaption => "MEMBER_CAPTION",
            Self::LevelUniqueName => "LEVEL_UNIQUE_NAME",
            Self::LevelNumber => "LEVEL_NUMBER",
            Self::ChildrenCardinality => "CHILDREN_CARDINALITY",
            Self::ParentLevel => "PARENT_LEVEL",
            Self::ParentCount => "PARENT_COUNT",
            Self::MemberType => "MEMBER_TYPE",
        }
    }

    pub fn from_name(name: &str) -> Option<Self> {
        Some(match name {
            "PARENT_UNIQUE_NAME" => Self::ParentUniqueName,
            "HIERARCHY_UNIQUE_NAME" => Self::HierarchyUniqueName,
            "MEMBER_UNIQUE_NAME" => Self::MemberUniqueName,
            "MEMBER_CAPTION" => Self::MemberCaption,
            "LEVEL_UNIQUE_NAME" => Self::LevelUniqueName,
            "LEVEL_NUMBER" => Self::LevelNumber,
            "CHILDREN_CARDINALITY" => Self::ChildrenCardinality,
            "PARENT_LEVEL" => Self::ParentLevel,
            "PARENT_COUNT" => Self::ParentCount,
            "MEMBER_TYPE" => Self::MemberType,
            _ => return None,
        })
    }
}

/// The members of one hierarchy's level read from an Arrow string column, in order, optionally after its All member.
/// Nothing is built per value: the text of each member is written from the column. The unique name of a value is
/// `<hierarchy>.&[<key>]` (`]` doubled), its caption the key, a null one has the key "" and `blank_caption`.
#[derive(Clone, Debug)]
pub struct LevelAxis<'a> {
    pub all: Option<Member>,
    /// A value's member without its names: `hierarchy`, `level_name`, `level_number`, `parent_unique_name`,
    /// `children_cardinality` and `member_type` are used; `unique_name` and `caption` are made per value.
    pub template: Member,
    /// A text array (`Utf8`, `LargeUtf8` or `Utf8View`).
    pub keys: Array<'a>,
    pub blank_caption: String,
}

/// Explicit tuples: the distinct members once, each tuple as indices into them (all tuples of one width).
/// `DisplayInfo` (children, drilled down, same parent) is worked out from them.
#[derive(Clone, Debug, Default)]
pub struct Tuples {
    pub members: Vec<Member>,
    pub tuples: Vec<Vec<u32>>,
}

impl Tuples {
    /// From tuples of members: equal members are stored once.
    pub fn from_members(tuples: Vec<Vec<Member>>) -> Self {
        let mut index: std::collections::HashMap<Member, u32> = std::collections::HashMap::new();
        let mut members = vec![];
        let tuples = tuples
            .into_iter()
            .map(|t| {
                t.into_iter()
                    .map(|m| {
                        *index.entry(m.clone()).or_insert_with(|| {
                            members.push(m);
                            (members.len() - 1) as u32
                        })
                    })
                    .collect()
            })
            .collect();
        Self { members, tuples }
    }
}

#[derive(Clone, Debug)]
pub enum AxisSource<'a> {
    Tuples(Tuples),
    Level(Box<LevelAxis<'a>>),
}

#[derive(Clone, Debug)]
pub struct Axis<'a> {
    pub source: AxisSource<'a>,
    pub properties: Vec<MemberProp>,
}

/// A cell property the client asked for; the header declares it, each cell carries `Value`, `FormatString` and
/// `Language` when asked.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CellProperty {
    pub name: String,
    pub element: String,
    pub xsd_type: Option<String>,
}

impl CellProperty {
    /// The standard properties by their MDX name (`VALUE`, `FORMAT_STRING`, ...).
    pub fn standard(name: &str) -> Option<Self> {
        let (element, ty) = match name {
            "VALUE" => ("Value", None),
            "FORMAT_STRING" => ("FormatString", Some("xsd:string")),
            "FORMATTED_VALUE" => ("FmtValue", Some("xsd:string")),
            "LANGUAGE" => ("Language", Some("xsd:unsignedInt")),
            "CELL_ORDINAL" => ("CellOrdinal", Some("xsd:unsignedInt")),
            "BACK_COLOR" => ("BackColor", Some("xsd:unsignedInt")),
            "FORE_COLOR" => ("ForeColor", Some("xsd:unsignedInt")),
            "FONT_NAME" => ("FontName", Some("xsd:string")),
            "FONT_SIZE" => ("FontSize", Some("xsd:unsignedShort")),
            "FONT_FLAGS" => ("FontFlags", Some("xsd:int")),
            "UPDATEABLE" => ("Updateable", Some("xsd:unsignedInt")),
            "ACTION_TYPE" => ("ActionType", Some("xsd:unsignedInt")),
            _ => return None,
        };
        Some(Self { name: name.into(), element: element.into(), xsd_type: ty.map(Into::into) })
    }
}

/// One measure's values, one per row, and its number format. Nulls are cells with no value (not written).
#[derive(Clone, Debug)]
pub struct Column<'a> {
    /// Float32/64, any signed or unsigned integer, or text.
    pub values: Array<'a>,
    pub format: String,
}

/// A whole cellset (the answer to an MDX `Execute`). A cell's ordinal is `row * columns + column`.
#[derive(Clone, Debug)]
pub struct Cellset<'a> {
    pub cube_name: String,
    pub axes: Vec<Axis<'a>>,
    pub slicer: Vec<Member>,
    pub cell_properties: Vec<CellProperty>,
    pub columns: Vec<Column<'a>>,
    /// The LCID written in `Language` (1033: English).
    pub language: u32,
}

impl Cellset<'_> {
    pub fn new(cube_name: impl Into<String>) -> Self {
        Self {
            cube_name: cube_name.into(),
            axes: vec![],
            slicer: vec![],
            cell_properties: ["VALUE", "FORMAT_STRING"].iter().filter_map(|n| CellProperty::standard(n)).collect(),
            columns: vec![],
            language: 1033,
        }
    }
}
