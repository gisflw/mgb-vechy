//! Shared aggregation attributes and raster-grid records.
pub(crate) const ATTRIBUTES: [&str; 8] = [
    "id",
    "id_down",
    "sub",
    "p_order",
    "unit_length",
    "upstream_length",
    "unit_area",
    "upstream_area",
];
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Attributes {
    pub integers: [i64; 4],
    pub metrics: [f64; 4],
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct Window {
    pub x: usize,
    pub y: usize,
    pub width: usize,
    pub height: usize,
}

pub(crate) struct Grid {
    pub transform: [f64; 6],
    pub width: usize,
    pub height: usize,
    pub wkt: String,
}

/// Source IDs keep their value and type; scientific ties use their string form.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub(crate) enum SourceId {
    Integer(i64),
    Real(u64),
    Text(String),
}

impl SourceId {
    pub(crate) fn key(&self) -> String {
        match self {
            Self::Integer(value) => value.to_string(),
            Self::Real(bits) => {
                let value = f64::from_bits(*bits);
                // Match Python's source-ID string ties, including its scientific-notation boundary.
                let text = if value != 0. && (value.abs() < 1e-4 || value.abs() >= 1e16) {
                    format!("{value:e}")
                } else {
                    format!("{value:?}")
                };
                if let Some((mantissa, exponent)) = text.split_once('e') {
                    let exponent: i32 = exponent.parse().expect("numeric exponent");
                    format!("{mantissa}e{exponent:+03}")
                } else {
                    text
                }
            }
            Self::Text(value) => value.clone(),
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct RoiAttributes {
    pub id: SourceId,
    pub id_down: Option<SourceId>,
    pub sub: i64,
    pub strahler_order: i64,
    pub metrics: [f64; 4],
    pub water_course: SourceId,
}

pub(crate) fn topological_order(
    ids: &[SourceId],
    downstream: &[Option<usize>],
) -> anyhow::Result<Vec<usize>> {
    use std::{cmp::Reverse, collections::BinaryHeap};
    let mut counts = vec![0; ids.len()];
    for target in downstream.iter().flatten() {
        counts[*target] += 1;
    }
    let mut ready = BinaryHeap::new();
    for (i, count) in counts.iter().enumerate() {
        if *count == 0 {
            ready.push(Reverse((ids[i].key(), i)));
        }
    }
    let mut order = Vec::with_capacity(ids.len());
    while let Some(Reverse((_, i))) = ready.pop() {
        order.push(i);
        if let Some(target) = downstream[i] {
            counts[target] -= 1;
            if counts[target] == 0 {
                ready.push(Reverse((ids[target].key(), target)));
            }
        }
    }
    anyhow::ensure!(order.len() == ids.len(), "Detected topology cycle");
    Ok(order)
}
