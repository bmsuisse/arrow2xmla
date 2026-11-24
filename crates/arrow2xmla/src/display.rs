//! `DISPLAY_INFO` of each member of each tuple: how a client lays the axis out (children, drilled down, same parent).

use crate::model::Member;

/// `DISPLAY_INFO` flag: the member is drilled down (its children follow).
pub const DRILLED_DOWN: u32 = 0x10000;
/// `DISPLAY_INFO` flag: the member has the parent of the one before.
pub const SAME_PARENT: u32 = 0x20000;

/// The children count (low 16 bits) of a member, as `DISPLAY_INFO` holds it.
pub fn children_bits(m: &Member) -> u32 {
    m.children_cardinality.min(0xFFFF)
}

/// A member is drilled down when the next tuple that does not repeat it (same outer members) holds one of its
/// children; it has the same parent as the previous one likewise. "Next" and "previous" skip the tuples that repeat
/// the member (the inner members vary under it). `tuples` hold indices into `members`, all of one width; the result
/// is flat, `tuples.len() * width` values, a tuple after another.
pub fn display_infos(members: &[Member], tuples: &[Vec<u32>]) -> Vec<u32> {
    let n = tuples.len();
    let width = tuples.first().map_or(0, Vec::len);
    let m = |i: usize, j: usize| &members[tuples[i][j] as usize];
    let mut infos: Vec<u32> = tuples.iter().flat_map(|t| t.iter().map(|&k| children_bits(&members[k as usize]))).collect();
    // same[i]: how many leading members tuple i shares with tuple i - 1
    let mut same = vec![0usize; n];
    for i in 1..n {
        let mut k = 0;
        while k < width && (tuples[i - 1][k] == tuples[i][k] || m(i - 1, k).unique_name == m(i, k).unique_name) {
            k += 1;
        }
        same[i] = k;
    }
    let mut start = vec![0usize; n];
    let mut end = vec![n; n];
    for j in 0..width {
        // start[i]: the first tuple of the run that repeats members 0..j; end[i]: the first tuple after that run
        for i in 1..n {
            start[i] = if same[i] > j { start[i - 1] } else { i };
        }
        end[n.saturating_sub(1)..].fill(n);
        for i in (0..n.saturating_sub(1)).rev() {
            end[i] = if same[i + 1] > j { end[i + 1] } else { i + 1 };
        }
        for i in 0..n {
            let me = m(i, j);
            let e = end[i];
            if e < n && same[e] >= j && m(e, j).parent_unique_name == me.unique_name {
                infos[i * width + j] |= DRILLED_DOWN;
            }
            let b = start[i];
            if b > 0 && !me.parent_unique_name.is_empty() && same[b] >= j && m(b - 1, j).parent_unique_name == me.parent_unique_name {
                infos[i * width + j] |= SAME_PARENT;
            }
        }
    }
    infos
}
