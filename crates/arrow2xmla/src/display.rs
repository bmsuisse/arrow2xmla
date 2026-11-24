//! `DISPLAY_INFO` of each member of each tuple: how a client lays the axis out (children, drilled down, same parent).

use crate::model::Member;

pub const DRILLED_DOWN: u32 = 0x10000;
pub const SAME_PARENT: u32 = 0x20000;

/// The children count (low 16 bits) of a member, as `DISPLAY_INFO` holds it.
pub fn children_bits(m: &Member) -> u32 {
    m.children_cardinality.min(0xFFFF)
}

/// A member is drilled down when the next tuple that does not repeat it (same outer members) holds one of its
/// children; it has the same parent as the previous one likewise. "Next" and "previous" skip the tuples that repeat
/// the member (the inner members vary under it).
pub fn display_infos(axis: &[Vec<Member>]) -> Vec<Vec<u32>> {
    let n = axis.len();
    let width = axis.first().map_or(0, Vec::len);
    let mut infos: Vec<Vec<u32>> = axis.iter().map(|t| t.iter().map(children_bits).collect()).collect();
    // same[i]: how many leading members tuple i shares with tuple i - 1
    let mut same = vec![0usize; n];
    for i in 1..n {
        let mut k = 0;
        while k < width && axis[i - 1][k].unique_name == axis[i][k].unique_name {
            k += 1;
        }
        same[i] = k;
    }
    for j in 0..width {
        let mut start = vec![0usize; n];
        for i in 1..n {
            start[i] = if same[i] > j { start[i - 1] } else { i };
        }
        let mut end = vec![n; n];
        for i in (0..n.saturating_sub(1)).rev() {
            end[i] = if same[i + 1] > j { end[i + 1] } else { i + 1 };
        }
        for i in 0..n {
            let m = &axis[i][j];
            let e = end[i];
            if e < n && same[e] >= j && axis[e][j].parent_unique_name == m.unique_name {
                infos[i][j] |= DRILLED_DOWN;
            }
            let b = start[i];
            if b > 0 && !m.parent_unique_name.is_empty() && same[b] >= j && axis[b - 1][j].parent_unique_name == m.parent_unique_name {
                infos[i][j] |= SAME_PARENT;
            }
        }
    }
    infos
}
