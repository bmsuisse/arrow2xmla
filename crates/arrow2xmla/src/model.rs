//! The data model: what a cellset is made of. No engine, no product names: a caller fills these in from wherever its
//! members and numbers come from.

use crate::array::Array;

/// One member of a hierarchy (a value of a field, its All member, a measure).
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub struct Member {
    /// The hierarchy's unique name, e.g. `[Customer].[Name]`.
    pub hierarchy: String,
    /// The member's unique name.
    pub unique_name: String,
    /// The text shown for the member.
    pub caption: String,
    /// The unique name of the member's level.
    pub level_name: String,
    /// The level's depth in its hierarchy (0: the All level).
    pub level_number: i32,
    /// "" for the All member and for a measure.
    pub parent_unique_name: String,
    /// How many children the member has.
    pub children_cardinality: u32,
    /// What the member is.
    pub member_type: MemberType,
}

impl Member {
    /// A member of a level; set the other fields (they are public) as needed.
    pub fn new(hierarchy: impl Into<String>, level_name: impl Into<String>, level_number: i32) -> Self {
        Self { hierarchy: hierarchy.into(), level_name: level_name.into(), level_number, ..Self::default() }
    }
}

/// MDSCHEMA `MEMBER_TYPE`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
#[non_exhaustive]
pub enum MemberType {
    /// A value of a field.
    #[default]
    Regular,
    /// The All member.
    All,
    /// A measure.
    Measure,
    /// A calculated member.
    Formula,
}

impl MemberType {
    /// The number XMLA gives it.
    pub fn code(self) -> u8 {
        match self {
            Self::Regular => 1,
            Self::All => 2,
            Self::Measure => 3,
            Self::Formula => 4,
        }
    }

    /// From the number; `None` when it is not one of the four.
    pub fn from_code(code: u8) -> Option<Self> {
        Some(match code {
            1 => Self::Regular,
            2 => Self::All,
            3 => Self::Measure,
            4 => Self::Formula,
            _ => return None,
        })
    }
}

/// A member property a client may ask for (`DIMENSION PROPERTIES`), written after each member of its axis.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum MemberProp {
    /// `PARENT_UNIQUE_NAME`
    ParentUniqueName,
    /// `HIERARCHY_UNIQUE_NAME`
    HierarchyUniqueName,
    /// `MEMBER_UNIQUE_NAME`
    MemberUniqueName,
    /// `MEMBER_CAPTION`
    MemberCaption,
    /// `LEVEL_UNIQUE_NAME`
    LevelUniqueName,
    /// `LEVEL_NUMBER`
    LevelNumber,
    /// `CHILDREN_CARDINALITY`
    ChildrenCardinality,
    /// `PARENT_LEVEL`
    ParentLevel,
    /// `PARENT_COUNT`
    ParentCount,
    /// `MEMBER_TYPE`
    MemberType,
}

impl MemberProp {
    /// The MDX name, e.g. `MEMBER_CAPTION`.
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

    /// From the MDX name; `None` when it is not one.
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
#[non_exhaustive]
pub struct LevelAxis<'a> {
    /// The All member, shown first.
    pub all: Option<Member>,
    /// A value's member without its names: `hierarchy`, `level_name`, `level_number`, `parent_unique_name`,
    /// `children_cardinality` and `member_type` are used; `unique_name` and `caption` are made per value.
    pub template: Member,
    /// A text array (`Utf8`, `LargeUtf8` or `Utf8View`).
    pub keys: Array<'a>,
    /// The caption of a null key.
    pub blank_caption: String,
}

impl<'a> LevelAxis<'a> {
    /// The values of `keys` as members made after `template`; no All member, blank caption `(blank)`.
    pub fn new(template: Member, keys: Array<'a>) -> Self {
        Self { all: None, template, keys, blank_caption: "(blank)".into() }
    }
}

/// Explicit tuples: the distinct members once, each tuple as indices into them (all tuples of one width).
/// `DisplayInfo` (children, drilled down, same parent) is worked out from them.
#[derive(Clone, Debug, Default)]
#[non_exhaustive]
pub struct Tuples {
    /// The distinct members.
    pub members: Vec<Member>,
    /// Each tuple as indices into `members`.
    pub tuples: Vec<Vec<u32>>,
}

impl Tuples {
    /// From the distinct members and the tuples naming them by index.
    pub fn new(members: Vec<Member>, tuples: Vec<Vec<u32>>) -> Self {
        Self { members, tuples }
    }

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

/// Where an axis's members come from.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub enum AxisSource<'a> {
    /// Explicit tuples.
    Tuples(Tuples),
    /// The values of one level, read from a column.
    Level(Box<LevelAxis<'a>>),
}

/// One axis of the cellset and the member properties written for it.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct Axis<'a> {
    /// The tuples.
    pub source: AxisSource<'a>,
    /// `DIMENSION PROPERTIES`, in order.
    pub properties: Vec<MemberProp>,
}

impl<'a> Axis<'a> {
    /// An axis of explicit tuples, without member properties.
    pub fn tuples(t: Tuples) -> Self {
        Self { source: AxisSource::Tuples(t), properties: vec![] }
    }

    /// An axis of one level's values, without member properties.
    pub fn level(l: LevelAxis<'a>) -> Self {
        Self { source: AxisSource::Level(Box::new(l)), properties: vec![] }
    }
}

/// A cell property written in each cell (declared in the header). Only the ones a writer produces are listed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[non_exhaustive]
pub enum CellProp {
    /// `VALUE`: the cell's value.
    Value,
    /// `FORMAT_STRING`: the number format of its column.
    FormatString,
    /// `LANGUAGE`: the LCID of the cellset.
    Language,
}

impl CellProp {
    /// The MDX name.
    pub fn name(self) -> &'static str {
        match self {
            Self::Value => "VALUE",
            Self::FormatString => "FORMAT_STRING",
            Self::Language => "LANGUAGE",
        }
    }

    /// The element it is written as.
    pub fn element(self) -> &'static str {
        match self {
            Self::Value => "Value",
            Self::FormatString => "FormatString",
            Self::Language => "Language",
        }
    }

    /// The type declared in the header (`Value` is typed per cell).
    pub fn xsd_type(self) -> Option<&'static str> {
        match self {
            Self::Value => None,
            Self::FormatString => Some("xsd:string"),
            Self::Language => Some("xsd:unsignedInt"),
        }
    }

    /// From the MDX name; `None` for a property this crate does not write.
    pub fn from_name(name: &str) -> Option<Self> {
        Some(match name {
            "VALUE" => Self::Value,
            "FORMAT_STRING" => Self::FormatString,
            "LANGUAGE" => Self::Language,
            _ => return None,
        })
    }
}

/// One column of cells: a value per row and the number format. Nulls are cells with no value (not written).
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct Column<'a> {
    /// Float16/32/64, any signed or unsigned integer, or text.
    pub values: Array<'a>,
    /// The number format (`FORMAT_STRING`).
    pub format: String,
}

impl<'a> Column<'a> {
    /// A column of cells.
    pub fn new(values: Array<'a>, format: impl Into<String>) -> Self {
        Self { values, format: format.into() }
    }
}

/// A whole cellset (the answer to an MDX `Execute`). A cell's ordinal is `row * columns + column`.
///
/// Axes: none (at most one cell), one (`rows * columns` tuples, the cells in order) or two (the first lists the
/// columns, the second the rows). More are refused.
#[derive(Clone, Debug)]
#[non_exhaustive]
pub struct Cellset<'a> {
    /// The cube's name.
    pub cube_name: String,
    /// At most two: columns, then rows.
    pub axes: Vec<Axis<'a>>,
    /// The members the whole answer is cut by (`WHERE`).
    pub slicer: Vec<Member>,
    /// What is written in each cell.
    pub cell_properties: Vec<CellProp>,
    /// The grid of cells, column by column.
    pub columns: Vec<Column<'a>>,
    /// The LCID written in `Language` (1033: English).
    pub language: u32,
}

impl Cellset<'_> {
    /// An empty cellset of a cube: value and format string in each cell, language 1033.
    pub fn new(cube_name: impl Into<String>) -> Self {
        Self {
            cube_name: cube_name.into(),
            axes: vec![],
            slicer: vec![],
            cell_properties: vec![CellProp::Value, CellProp::FormatString],
            columns: vec![],
            language: 1033,
        }
    }
}
